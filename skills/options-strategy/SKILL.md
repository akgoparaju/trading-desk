---
name: options-strategy
description: Turn a directional view + the REAL options chain into concrete defined-risk STRUCTURES — real strikes only, economics minted from chain marks, probabilities shown as labeled delta approximations, and mechanical honesty gates. IV-vs-REALIZED is the PRIMARY GATE (never IV level alone). Use when the user says "options strategy [ticker] [bullish|bearish|neutral]", "options play [ticker]", or when invoked by trade-plan (pipeline). Pipeline mode derives direction from the composite grade and aligns to the stock plan; standalone mode requires an explicit --direction. Consumes the newest snapshot + its on-disk chain (never loaded into context). Rubric v1.1.0 (PROVISIONAL).
---

# Options Strategy (Decision Layer — Structure Selection)

Turn a **direction** and the **real options chain** into concrete, defined-risk option structures for a ticker. **All arithmetic is done by `scripts/options_strategy.py`** — you set the mode (and, standalone, the direction), run the script, read its JSON, and write prose. You never pick a strike, compute a credit, a breakeven, or a probability in text.

This is an **L3 structure-selection layer**. It reads the newest snapshot's options/sentiment blocks and the on-disk chain (via `chain.load_contracts` **only** — the chain is NEVER loaded into your context) and emits `module_options.json`. It scores **no** snapshot field directly (`INPUT_FIELDS = set()`), so single-mapping is preserved by construction.

**The one lesson this module encodes:** **IV LEVEL alone never selects a strategy — IV-vs-REALIZED is the primary gate.** In the prototype session a 96% IV *looked* rich, but it sat ~14 points **below** ~110–116% realized. That IV was **cheap vs realized**, not rich — premium sellers were not being paid for delivered vol, and a naive "sell premium" call would have been wrong. The script gates the entire selection matrix on `options.iv_minus_rv20`, not on the IV percentile.

**Non-negotiables:**
- **Never do arithmetic in prose.** Every strike, credit/debit, max profit/loss, breakeven, and PoP you cite must already appear in `module_options.json` (`recommended_structures[].arithmetic`, `.pop`, `.breakevens`, etc.). A number you would have to compute is a script change, not a prose change.
- **Real strikes only.** Strikes come from the actual chain at the chosen expiry, selected by delta. There are no illustrative round-number strikes.
- **PoP is a labeled approximation.** Every probability is `1 − |Δ short|` (credit) or `|Δ long|` (debit) — a delta-as-ITM-probability proxy, not a model price. The `pop_method` string names the approximation; carry it.
- **Declined structures are reported, never forced.** A structure that fails the liquidity gate or has no vol edge is listed in `declined` with its reason. If the chain is thin, the module says so (`liquidity_verdict`) rather than manufacturing a trade.

Trigger phrases: "options strategy for MU bullish", "options play NVDA", "what options structure for AAPL". Invoked automatically by **trade-plan** in pipeline mode.

---

## Step 1 — Locate the bundle (need a snapshot with a chain)

In the invoker's CWD, find the newest bundle for the ticker:

```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Newest first across both layouts: the new `./trading_desk_<TICKER>/detail_reports_<date>/` bundles and the legacy `./td_bundle_<TICKER>_<date>/` bundles (fallback for old runs).

- **If NO bundle (or no snapshot with a chain) exists**, invoke the **market-snapshot** skill for `<TICKER>` first — options-strategy needs the on-disk chain that market-snapshot writes and references at `snapshot.options.chain_file_path`.
- The script resolves that chain file **relative to the bundle** and loads it **only** through `chain.load_contracts`. If the chain is missing or unreadable, the script exits 2.

---

## Step 2 — Choose the mode

- **Pipeline mode** (`--mode pipeline`) — use this when **called from trade-plan** (i.e. `module_composite.json` AND `module_tradeplan.json` are both present). Direction is derived from the composite grade (A|B → bullish, C → neutral, D → bearish); the CSP is aligned to the stock plan's `entry_1`; the hedge is built from the stock plan's hedge spec. **Both files are required** (exit 2 if either is missing).
- **Standalone mode** (`--mode standalone --direction bullish|bearish|neutral`) — use this for a direct "options strategy [ticker] [direction]" request. `--direction` is **required** (exit 2 if absent); no composite/trade-plan needed. Be honest about the direction — a neutral view on cheap-vs-realized vol will (correctly) decline to sell premium.

---

## Step 3 — Run the script

```bash
# pipeline (invoked by trade-plan):
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/options_strategy.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> --mode pipeline

# standalone:
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/options_strategy.py \
  --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD> --mode standalone --direction bullish
```

The script writes `<bundle>/module_options.json` (path printed to stdout). **How it's built (all mechanical):**
- **Vol dashboard (PRIMARY GATE)** — `vol_verdict(options.iv_minus_rv20)`: `≤ −0.03` → **cheap_vs_realized** (no premium edge; long premium viable), `≥ +0.03` → **rich_vs_realized** (premium selling favored), between → **fair**, null → **unknown** (treated as fair + disclosed). Plus iv30, rv20, iv_pctile, term structure (front-vs-back ATM IV), and 25d skew.
- **Expiry** — monthlies preferred (3rd-Friday heuristic). Pipeline with a catalyst ≤ 60 DTE → first monthlyish expiry **after** the catalyst; else the expiry nearest **45 DTE** within [30, 90].
- **Strikes by delta off the real chain** — short put/call ≈ 0.30Δ; long call ≈ 0.55Δ; wings 1–2 strikes out; condor shorts ≈ 0.25Δ each side. Pipeline: if `entry_1` is within 2% of a listed put strike, the CSP is rebuilt at **that** strike (labeled "aligned to stock-plan entry_1").
- **Selection matrix (direction × vol verdict)** — bullish×rich/fair → bull_put_spread + cash_secured_put; bullish×cheap → long_call_vertical (+ bull_put_spread with warning); bearish×rich/fair → bear_call_spread; bearish×cheap → long_put_vertical (+ bear_call_spread with warning); neutral×rich → iron_condor; **neutral×cheap/fair → NO premium structure** (a `declined` "stand aside" entry).
- **Economics** — from chain marks (mid = mark, fallback (bid+ask)/2): net credit/debit, max profit/loss, breakevens, and PoP with a named `pop_method`, all round-tripped in an `arithmetic` string.
- **Iron-condor honesty check** — if the profit zone's half-width sits **inside** the snapshot 1σ expected move, a warning fires and `pop_full_profit_note` is set (full-profit probability is LOW — a bet that realized vol cools).
- **Liquidity gate** (per leg) — `oi ≥ 100` AND `spread ≤ max(0.10, 0.10×mark)`; a failing leg moves the structure to `declined`. Fewer than 2 viable structures → `liquidity_verdict: "thin — declining to force structures"`.
- **Honesty gates** — cheap-vs-realized tags every credit structure ("premium sellers are NOT being paid for delivered vol"); earnings ≤ 30d excludes the CSP + tags all structures ("IV-crush/defined-risk-only into event"); ex-div within tenor tags short-call legs (early-assignment risk).
- **Skew-informed routing (Wave 4B, PROVISIONAL)** — `skew_verdict` reads the 25Δ risk-reversal `rr_25d = IV(25Δ put) − IV(25Δ call)` at the **selected working expiry** (`chain.skew_25d`, not just the fixed 30d): `rr > +0.04` → **puts_rich** (downside skew/fear) → prefer **SELLING puts** (bull-put / CSP) over buying calls even in the cheap regime; `rr < −0.04` → **calls_rich** → prefer **selling calls**; else **balanced**; null → **unknown**. For the iron condor, the rich wing's short is sold nearer the money (~0.30Δ) and the **cheap wing is widened** (short pushed further OTM ~0.20Δ). The module emits `skew_verdict` + `skew_rr_25d`. **0.04 is a PROVISIONAL default** (equity RR typically spans 0.01–0.10).
- **Candidate breadth (Wave 4B)** — the matrix is expanded so **bearish/rich also tries a debit-put-vertical fallback** (not only the credit spread); short strikes **retry an adjacent delta (0.25/0.35)** when the delta pick is illiquid; if **every** candidate at the primary expiry fails the liquidity gate, the same specs are tried **once at the next listed expiry**. `candidates_tried` (count) is emitted so the breadth is visible ("tried N, all declined for <reasons>" instead of standing aside after one).
- **Crush simulation (Wave 4B, PROVISIONAL)** — for each candidate **when earnings fall within the structure's horizon**, each leg is re-priced with the **verified** `chain.bs_price(S, K, T_post, r, iv_leg × IV_CRUSH_FACTOR, opt_type)` at scenario spots **±1σ/±2σ** (from `expected_moves.one_sigma`), where `T_post` = DTE **after** the print (years) and `r` = `macro.treasury_10y/100` or 0. `IV_CRUSH_FACTOR = 0.62` (cited: ~38% avg post-earnings IV crush). Legs are netted per scenario, and `crush_ev = Σ scenario_prob × PnL` over the **symmetric ±1σ/±2σ weight set** `(0.05, 0.24, 0.42, 0.24, 0.05)`. Each candidate carries `crush_ev` + `survives_crush`; an **event-window structure with `crush_ev ≤ 0` is DECLINED** with reason "negative crush-adjusted EV". Non-event structures skip the crush gate (`crush_ev: null`, `survives_crush: true`).
- **Management rules** — per structure family (credit: 50% target / 2× stop / 21 DTE; condor: 25–35% / roll untested side; debit: 100% gain / −50% stop / 21 DTE).
- **Hedge** (pipeline, if the stock plan's hedge is required) — a put spread from the hedge `strikes_from`; if cost/spot exceeds the premium cap, a **collar alternative** (short call ≈ 0.20Δ) is emitted and disclosed.

---

## Step 4 — Write the brief

Read `module_options.json` (small — read it directly). Write `<bundle>/brief_options.md`. **Every number comes from the JSON/snapshot — zero computed-in-prose numbers.** Format discipline (uniform with the evidence briefs): open with a verdict headline line (`### Options — {vol verdict} / {n} structures (rubric v{ver})`), round displayed floats to 2 dp in prose and tables (full precision stays in the JSON), and END with a one-line `**Signal:**` read — the disclaimer goes below the signal, never in place of it.

**Headline first (the gate):** one **bold** line stating the IV-vs-realized verdict — e.g. **"IV 96% but 14 pts BELOW realized → cheap_vs_realized: premium selling has no edge here."** This is the decision, not decoration.

Then, in order:
- **Vol dashboard table** — verdict, iv30, rv20, diff, iv_pctile, term structure, skew (verbatim from `vol_dashboard`).
- **Expected-move table** — from `expected_moves` (straddle, 1σ, range) per expiry.
- **Strategy table (2–4 structures)** — one row each: legs (strike/side), credit/debit, max profit / max loss, breakevens, **PoP with its method**. Numbers verbatim from `recommended_structures[]`.
- **Management rules** — per structure, from `.management`.
- **Hedge** — if `hedge_structure` is present: legs, cost, and the collar alternative if one was emitted.
- **Warnings** — reproduce `warnings_global` and each structure's `warnings` **verbatim**; do not soften them.
- **Declined** — list `declined[]` (name + reason) so the reader sees what was considered and why it was passed.

**Footer** — `_Rubric v<rubric_version> · IV-vs-realized primary gate · as of <as_of>_`.

---

## Step 5 — Output contract

Report to the user (and to any calling skill):
- **Module path** — `<bundle>/module_options.json`
- **Brief path** — `<bundle>/brief_options.md`
- **Vol verdict** — the IV-vs-realized gate (the headline)
- **Direction** — and its source (composite grade or flag)
- **Recommended structures** — names + strikes + PoP (with method)
- **Declined** — and why (thin liquidity, no vol edge, event exclusion)
- **Hedge** — put spread or collar, if emitted

---

## Important Notes

- **IV-vs-realized is the primary gate — never IV level alone.** A high IV percentile does not mean premium is rich. If realized vol exceeds implied, sellers are not being paid for the vol they will deliver; the script refuses to assert a premium-selling edge there and tilts toward long-premium / defined-risk directional structures. State this generically (the prototype's 96%-IV-below-realized case is the canonical example).
- **The chain never enters your context.** The script is the only reader; it emits compact derived structures. Do not open the chain file or quote raw contracts — cite the module JSON.
- **PoP is a delta approximation, always labeled.** `1 − |Δ short|` (credit) or `|Δ long|` (debit) is a proxy for the probability of finishing on the profitable side, not a priced probability. Carry the `pop_method` string; never present PoP as exact.
- **Declined structures are reported, not forced.** A thin chain, a failed liquidity leg, an event exclusion, or "no vol edge" is a legitimate output — the honest answer is sometimes "stand aside." Never manufacture a structure to fill the table.
- **Rubric version travels with the numbers.** `rubric_version` (`1.1.0`, **PROVISIONAL**) prints in the JSON and must appear in the brief footer.
- **Wave 4B thresholds are PROVISIONAL (Philosophy A) with a stated falsifier.** The crush-EV gate (`IV_CRUSH_FACTOR = 0.62`), the crush scenario weights, and the `0.04` skew-routing threshold are cited but unproven on this codebase's own earnings sample. **Falsifier:** *if across the B9 set the crush-EV gate declines structures that would have been profitable (crush factor too aggressive) or passes structures that lose on realized crush (too lax), the 0.62 factor is refuted and re-set — ideally calibrated from bracketing IV-history samples once enough earnings are captured; if the 0.04 skew threshold routes to worse-performing structures than the base matrix, it is refuted.* Carry `crush_ev` / `survives_crush` / `skew_verdict` verbatim from the JSON; never present the crush EV or the skew route as validated. **B9 update (2026-07-23):** the crush gate was exercised numerically on 3/10 calibration names (all *survived* — the decline-a-losing-structure path was not reached, since thin-liquidity names decline on the liquidity gate first) and the skew router produced non-degenerate verdicts; the structural falsifier did not trip, but the 0.62 / 0.04 thresholds' predictiveness stays forward-tracking (the crush-decline and calls-rich / extreme-skew paths remain unexercised).
- **Event-vol / crush / skew / breadth are disclosures, not decorations.** The event-vol read (`options.event_vol` — the isolated earnings-day 1σ via bracketing-expiry variance additivity) and the ex-earnings RV (`options.rv20_ex_earnings`) are richer event-name signals than the blunt iv30-vs-rv20; the crush sim PRICES the post-print vol collapse rather than narrating it; skew routing sells the rich wing; candidate breadth reports `candidates_tried`. Reproduce these in the brief so the reader sees the reasoning.
- **Educational only.** This is analysis, not investment advice; options carry defined but real loss risk, and the delta-based probabilities are approximations. Say so.
- **Snapshot and upstream modules are read-only.** This skill writes only `module_options.json` and `brief_options.md`; it never edits the snapshot or any evidence/composite/trade-plan module JSON.
