# Spec — Wave 2: Event-Aware Risk (R1) — data + disclosure now, scoring gated on calibration

**Date:** 2026-07-20 · **Status:** Part A proposed (executable); **Part B GATED on a user calibration decision** · **Source:** `jutsu-trading-desk/docs/reviews/2026-07-19-analysis-quality-review.md` R1 + Part-3 Risk table; `.../2026-07-20-development-priorities.md` Wave 2 (B24).

**Verification note (no-guessing).** Two Explore passes mapped the current risk rubric and the snapshot's event/gap/news data at `file:line`. The split below is not a scoping preference — it is where the evidence runs out: the DATA computations are deterministic (95%); the SCORING re-weight + two of the six data signals are calibration/heuristic choices the review itself marks as the user's (B8/B9) and which cannot reach 95% without a decision. **I will not fabricate a weight vector, a short-seller entity list, or an EWMA half-life and call it 95%.**

---

## The gap being closed (why R1 exists)
The 2026-07-19 review's headline: the Hunterbrook short report + the earnings binary reached **zero rubric points** — the event lived only in prose. `score_risk.py` has no event/chain/news awareness (verified: it imports only `build_snapshot`, `confidence`, `levels`; the "binary event ≤30d" notch lives in `trade_plan.py:138`, not risk). R1 makes the event **real evidence**. Part A makes it **computed + surfaced**; Part B makes it **scored** (gated).

---

## Current risk rubric (verified, `score_risk.py`)
Four factors summing 100: `volatility_state` 25 (rv30-pctile 20 + beta 5), `drawdown_profile` 25 (max_dd 12 + episodes 8 + spread 5), `margin_of_safety` 30 (dist_from_ath 12 + asymmetry 18), `liquidity_solvency` 20 (ADV 10 + net-cash 10). Renormalization is generic over N factors (`score()` :642). Output carries `subscores`, `tables.{downside_map,vol_profile}`, `flags.{stress_pct,top_risk}`, `downside_floor_mode`, and (Wave 1) `confidence`. rubric_version `:89 = "1.0.0"`.

---

# PART A — Deterministic data + disclosure (EXECUTABLE, 95%, no score change)

**Contract: no scored subscore changes.** Risk's four factors, bands, and weights are untouched → every existing risk score is byte-identical. Part A only (i) adds deterministic snapshot fields, (ii) surfaces them as **unscored** context in the risk module + brief, (iii) fixes the valuation_floor mislabel, (iv) adds the governance doctrine line. Risk's SCORING rubric_version stays `1.0.0`; the module gains an unscored `event_context`/`tail_context` (module note `"event-context v1 (unscored)"`). **Confidence DEPTH for risk stays MEDIUM** (scoring not yet event-aware) — it promotes to HIGH only in Part B.

### A1 — Snapshot schema 0.2.2 → 0.3.0 (additive, deterministic fields)

**`events.days_to_event`** — integer days from `meta.as_of_utc[:10]` to `events.next_earnings.date`; null if no date. The exact subtraction already exists at `score_sentiment.py:642-659` — lift it. Slot: `build_events()` (build_snapshot.py:957), thread `as_of_date` in.

**`events.implied_move`** — copy the already-computed `sentiment.implied_move_next_earnings_pct` (build_snapshot.py:882, from `_implied_move_next_earnings()` :802 → `chain.expected_move` one_sigma) into the `events` block for locality. No new computation. (The `0.85×straddle` sigma convention at chain.py:255 is inherited + documented.)

**`events.earnings_move_history`** — list of up to 8 `{"quarter_end": <reportedDate>, "move_pct": <float>}` from the ticker's own last-8 reported quarters. Compute from `earnings.quarterlyEarnings[].reportedDate` (present in the raw file; only `reportedEPS` is read today, :484) + the parsed daily `rows` (`parse_daily_rows` keeps open + close + adjusted_close). **Reaction-window convention (documented, defensible — a measurement, not a calibration):** for a reportedDate D, `move_pct = close[first trading day ≥ D+1] / close[last trading day ≤ D−1] − 1` (spans the report, robust to BMO/AMC timing ambiguity when `time` is absent); if `next_earnings.time`/row time disambiguates BMO vs AMC, narrow to the single-session reaction. Missing OHLCV around a date → skip that quarter (recorded). New helper; thread `earn_q` + `rows` into a new `build_earnings_move_history(earn_q, rows)` called from `build_events` (or a top-level helper wired at build_snapshot.py:~1112).

**`events.implied_move_vs_own_history_pctile`** — deterministic: the percentile rank of `events.implied_move` within the abs values of `earnings_move_history[].move_pct`. Null if either input null. Tells the reader "the market is pricing an above/below-average reaction vs this name's own history."

**`technicals.overnight_gap`** — `{mean_abs, p95_abs, max_abs, excess_kurtosis, jump_count_2sigma, n}` from the overnight-gap series `open[i]/adjusted_close[i-1] − 1` over the retained daily `rows` (open is parsed at :227/:271 but currently unused downstream). `excess_kurtosis` = standard 4th-standardized-moment − 3 (deterministic). `jump_count_2sigma` = count of gaps with `|gap| > 2·std(gaps)` (the 2σ threshold is a **documented convention**, flagged). New functions in `indicators.py`; loop added in `build_technicals()` (build_snapshot.py:354), which already receives `rows`.

**Schema:** bump the snapshot schema version string to `0.3.0`; re-pin snapshot fixtures. All fields are additive and null-safe (degraded/web-fallback runs where the chain/earnings are absent → nulls, listed in `meta.missing`).

### A2 — Risk module: surface the data (UNSCORED) + fix presentation
In `score_risk.py` `build_module()` (doc assembled :729-753), **without touching any scored subscore**:
- Add `tables.event_context` = `{days_to_event, implied_move, implied_move_vs_own_history_pctile, earnings_move_history_summary}` read verbatim from the snapshot (zero arithmetic in the module — the snapshot computed them).
- Add `tables.tail_context` = the `technicals.overnight_gap` block, verbatim.
- Add these fields to `INPUT_FIELDS` as **context-only** (documented as unscored, so the single-mapping test knows they carry no points).
- Module note: `"event-context v1 (unscored) — surfaced for disclosure; scoring gated on calibration (Part B)"`.

### A3 — valuation_floor relabel (the review's "−97.7% anchor in a swing map")
The suspect-floor row (`valuation_floor()` :493, suspect logic :547) already flags `suspect: true`. Additionally, when the floor is a long-horizon `dcf_bear` (anchored mode) or a suspect snapshot floor sitting far below the nearest proven swing support, **relabel its `basis`** to `"long-horizon anchor (not a swing level)"` and segregate it in `build_downside_map()` (:562) so a reader cannot mistake a multi-year floor for an actionable swing level. Presentation only; the number is unchanged.

### A4 — Governance doctrine line (SKILL prose, no scoring)
Add to `skills/risk-analytics/SKILL.md` the doctrine the review named: **"Risk is a gate/governor, never a reward input — conviction never loosens a risk parameter."** And direct `top_risk`: when `events.days_to_event ≤ 30`, name the event explicitly and cite the `event_context` figures (the data now exists to cite). This is guidance for the LLM judgment flag, not a scored change.

### Part A tests
- `indicators.py`: overnight-gap series, excess kurtosis (against a known-kurtosis fixture), jump count at 2σ.
- `build_snapshot`: `days_to_event`, `implied_move` copy, `earnings_move_history` (a fixture with 8 reportedDates + daily rows → known moves; BMO/AMC/unknown handling; missing-date skip), `implied_move_vs_own_history_pctile`, `overnight_gap`. Schema-version re-pin to 0.3.0.
- `score_risk`: the scored `score` is **unchanged** on an existing fixture (regression: byte-identical subscores); `event_context`/`tail_context` present and equal to the snapshot values; valuation_floor relabel appears; single-mapping test updated for the context-only fields.
- Full suite green.

---

# PART B — Event-aware SCORING (GATED — needs your decision, NOT executed)

Part B turns the Part-A data into **risk points** and adds the two heuristic signals. Each item below states the specific decision required; none is fabricated.

### B-i — The scored re-weight (`risk-v1.1.0`) → **needs a calibration decision**
To score event-proximity/implied-move and gap/jump, the current 25/25/30/20 must be cut to make room (verified: not additive). **Decision needed:** the new weight vector + the per-factor bands. This is exactly the review's B8/B9 calibration territory (marked 👤, "provisional until 5-10 names scored"). Proposed *starting* shape for your review (NOT a default I'll ship unasked): event-proximity/implied-move as a ~15-pt factor (days-to-event bands × implied-vs-own-history), gap/jump as a ~10-pt factor, taken proportionally from vol/drawdown/MoS/liq — but the numbers are yours to set, ideally after a few anchored runs sit side by side (B9).

### B-ii — `sentiment.news_heat` → **needs a parameter decision**
Deterministic EWMA of relevance-weighted article sentiment + an article-volume z-spike, from the raw `news` feed (currently loaded but never parsed for numbers — CANONICAL_CONTRACT.md:173). **Decision needed:** decay half-life (review cites 2–5d; pick e.g. 3d), z-score lookback window, and whether to use `overall_sentiment_score` vs ticker-specific `ticker_sentiment_score`. Cheap to build once fixed; I won't pick the half-life for you.

### B-iii — `sentiment.short_campaign` → **needs heuristic sign-off (highest stakes)**
Flag + date + source, from a news-feed entity/source scan + SI corroboration. **Decision needed:** the short-seller/activist **entity list** (which publishers/funds count), the text-match patterns, and the SI corroboration threshold (e.g. `short_interest_pct > X% AND si_trend == rising`). A risk **gate** acting on a false positive is dangerous — this needs explicit sign-off, not a guessed list. (Note: the *deterministic* part of the Hunterbrook signal — a cluster of high-relevance strongly-negative recent articles — is captured by `news_heat` B-ii without an entity list; the *"this is an activist short campaign"* label is what needs the list.)

### B-iv — Confidence DEPTH promotion
When `risk-v1.1.0` ships (B-i), bump the `DEPTH_TABLE` row in `confidence.py`: `risk` at `1.1.0` → HIGH. One line, disclosed. (Not before — an event-blind risk score reading HIGH would be the dishonesty the confidence layer exists to prevent.)

---

## Constraints honored
- **Part A moves no score** (regression-pinned byte-identical); **single-snapshot** (risk reads snapshot fields, computes nothing); **script-is-rubric / zero LLM arithmetic** (all event/gap math in build_snapshot + indicators); **rubric/schema versions travel** (0.3.0 + the unscored module note); **renormalization** unaffected (context fields carry no points).
- **Part B** is deferred precisely because it cannot meet "no guesses / 95%" — it needs the calibration + heuristic decisions above.

## Definition of done (Part A)
Snapshot 0.3.0 carries the four deterministic event/tail fields; the risk module + brief surface them as unscored context with the event named in `top_risk` when ≤30d; the valuation_floor is relabeled as a long-horizon anchor; the doctrine line is in the SKILL; the risk **score is provably unchanged**; full suite green. **Then the loop pauses on Part B pending your calibration + heuristic decisions.**
