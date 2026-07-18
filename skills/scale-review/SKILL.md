---
name: scale-review
description: Review the active sector valuation scales — the versioned JSON contracts (trading_desk_config/scales/*.json) that anchor fundamental scoring to a sector's structure. For each active scale, gather fresh evidence, render a verdict (valid | erosion_suspected | rebasing_proposed), and when rebasing is warranted DRAFT a complete forward-versioned replacement that must survive an ADVERSARIAL refutation gate (≥2 of 3 independent refutation passes must fail to refute) before it is filed as a proposal. Use when the user says "review scales", "scale review", or on a scheduled invocation. NEVER applies a scale — it files a pending proposal; ratification is a separate one-word user action. Falsifier tripping is monitored per-refresh by scripts/refresh_plan.py; this skill is the deliberate periodic re-examination.
---

# Scale Review (Adversarial Proposal Gate)

A **sector scale** is a small, versioned JSON contract (`trading_desk_config/scales/<name>.json`) declaring how a sector's fair-value BAND is computed from first-principles fundamentals and the FALSIFIERS that would break its thesis. `score_fundamental` positions a name's multiple inside that band; a wrong scale silently distorts every fundamental score in the sector. This skill is the **deliberate, periodic re-examination** of those scales — distinct from the per-refresh falsifier monitoring `scripts/refresh_plan.py` already surfaces in every `refresh_plan.json` (`scales[]`, `scale_review_required`).

You are a **conductor with an adversary on retainer**: a scale is data of record, so you never tune it because a number feels off and you **never apply a change**. You gather fresh evidence, reach a verdict per scale, and — only when the evidence forces a re-base — DRAFT a complete forward-versioned replacement that must **survive independent refutation** before it is even filed as a *proposal*. Ratification (moving a proposal into `scales/`) is a separate, explicit, one-word user action.

**Non-negotiables:**
- **NEVER apply a scale.** This skill writes to `trading_desk_config/scales/proposals/` only. It never edits, overwrites, or replaces a file under `trading_desk_config/scales/`. The only path from proposal to active is the user typing the ratification command.
- **Refutation survival is the gate, not confidence.** A proposal is filed only if it survives an adversarial gate: 3 independent passes each PROMPTED TO REFUTE it, and ≥2 must fail to refute. Self-reported confidence ("I'm 90% sure") is never a gate — an unrefuted case is.
- **Forward-only versioning.** A re-base creates a NEW `<name>@<newversion>`; the current scale keeps its version and stays active until ratification. `prior` on the draft is set to the CURRENT scale (the Bayesian anchor the new parameters move from).
- **Auto-apply is reserved for pre-registered consequences.** The ONLY thing that may change a scale automatically is a falsifier's pre-registered `on_trip` consequence (handled elsewhere, and only `flag+disclose`-class). A parameter re-base is never auto-applied.
- **Evidence or silence.** Every drafted parameter carries a derivation and a cited source (a context finding `C<n>` or a URL). No invented ROE, growth, or discount rate.

Trigger phrases: "review scales", "scale review", "re-examine the sector scales", a scheduled/cron invocation.

---

## Step 1 — Enumerate active scales and their consumers

In the invoker's CWD:

```bash
ls trading_desk_config/scales/*.json 2>/dev/null
ls trading_desk_config/scales/proposals/*.json 2>/dev/null   # already-pending drafts
```

For each active scale, read it (`scale`, `version`, `formula`, `parameters`, `evidence`, `falsifiers`, `prior`, `on_trip`) and **map its consumers** — the tickers/workspaces whose fundamental score used it. Scan `./trading_desk_*/detail_reports_*/module_fundamental.json` for a `justified_band` / scale reference matching the scale name. A scale with live consumers is higher-stakes; note the count.

Surface any pending proposals from `proposals/` immediately: an unratified proposal is always reported (see the footer rule) so it is never silently forgotten.

---

## Step 2 — Gather fresh evidence per scale

For each active scale, assemble the current picture — do **not** rely on the numbers baked into the scale:

- **Latest bundle context** — the newest `module_context.json` `live_tape` for a representative consumer ticker (what is moving the sector NOW) and its `findings[]` you can cite by ID.
- **Coverage updates** — any FSI `model-update` / new reported quarter since the scale's `effective` date that revises the sector's normalized ROE, growth, or multiple.
- **Targeted web research** — sector forward-multiple prints, cost-of-equity / discount-rate shifts, structural changes (consolidation, new entrant, demand regime). Cite every source URL.

Record the evidence as you go; you will cite it in the verdict and, if you draft, in the proposal's `evidence[]`.

---

## Step 3 — Verdict per scale (cited reasoning)

Render exactly one verdict per scale, each with reasoning that cites the Step-2 evidence:

- **`valid`** — the scale's parameters and band still match fresh evidence; falsifiers untripped. No action.
- **`erosion_suspected`** — evidence is drifting from the scale's assumptions (a falsifier is close, a parameter is stale) but not yet decisively broken. Flag it, name the metric to watch, set a re-review date. **No draft** — suspicion is not a re-base.
- **`rebasing_proposed`** — evidence decisively contradicts a parameter or band; a re-base is warranted. Proceed to Step 4.

A tripped falsifier in the latest `refresh_plan.json` (`scale_review_required: true`) is a strong prompt toward `erosion_suspected` or `rebasing_proposed`, but the verdict still rests on your fresh evidence, not the trip alone.

---

## Step 4 — DRAFT the complete new version (only for `rebasing_proposed`)

Build the **full** replacement scale JSON — a partial draft is not a proposal. It must validate against the `sector_scales` contract (`scale`, `version`, `effective`, `basis`, `formula`, `parameters`, `evidence`, `falsifiers`, `prior`):

- **`version`** — a NEW forward version (e.g. bump the date); never reuse the current one.
- **`parameters`** — each with a stated **derivation** (how you got the normalized ROE / growth / discount rate / NAV multiple) and the arithmetic that yields the justified mid. `r > g` where the formula requires it.
- **`evidence`** — a list of `C<n>` finding IDs and/or source URLs backing each parameter. No uncited number.
- **`falsifiers`** — the dotted-metric conditions that would break THIS new thesis, each with a `meaning` and a **pre-registered `on_trip`** consequence (default `flag+disclose`). Include `consecutive_quarters` where a single print should not trip.
- **`prior`** — set to the CURRENT (soon-to-be-superseded) scale's parameters: the new band moves FROM the prior, and the delta must be justified by the evidence.

---

## Step 5 — ADVERSARIAL GATE (refutation survival)

Before filing, the draft must **survive refutation**. Dispatch **3 independent refutation passes**; if the `Agent` tool is available, launch all three in parallel, each with an independent prompt to **REFUTE** the proposal — otherwise emulate them **sequentially** as three separate, self-contained critiques (do not let one pass see another's conclusion).

Each pass is prompted adversarially:

> You are refuting a proposed re-base of the `<name>` sector scale. Here is the current scale, the proposed replacement, and its cited evidence. Find the strongest reason the re-base is WRONG: an uncited or misderived parameter, a band that overfits recent prints, a falsifier that is unfalsifiable or already stale, a `prior` the delta does not justify, or evidence that does not support the claimed magnitude. Return `REFUTED: <reason>` or `NOT_REFUTED: <why the case holds>`.

**Survival rule:** the proposal survives only if **≥2 of 3 passes return `NOT_REFUTED`**. Record all three votes verbatim in the proposal file's `votes[]`. If it does not survive, do NOT file — report the refutations to the user and either revise (re-run the gate) or downgrade the verdict to `erosion_suspected`.

---

## Step 6 — File the proposal (pending ratification)

Only a surviving draft is written, and only under `proposals/`:

```
trading_desk_config/scales/proposals/<name>_<version>.json
```

The file is the complete drafted scale plus the gate record:

```json
{
  "scale": "<name>", "version": "<newversion>", "effective": "<YYYY-MM-DD>",
  "basis": "...", "formula": "...", "parameters": { ... }, "evidence": [ ... ],
  "falsifiers": [ ... ], "prior": { ...current scale... },
  "status": "pending_ratification",
  "votes": [
    {"pass": 1, "verdict": "NOT_REFUTED", "reason": "..."},
    {"pass": 2, "verdict": "NOT_REFUTED", "reason": "..."},
    {"pass": 3, "verdict": "REFUTED", "reason": "..."}
  ],
  "drafted_utc": "<UTC timestamp>"
}
```

**NEVER apply it.** Tell the user the one-word ratification path in plain terms: typing **`ratify <name>@<version>`** moves the proposal into `trading_desk_config/scales/` (history retained — the superseded scale is archived, not deleted). Until then, the CURRENT scale stays active and every fundamental score keeps using it.

---

## Output contract

Report to the user:
- **Per-scale verdict table** — `<name>@<version>` → `valid | erosion_suspected | rebasing_proposed`, one-line cited reason, live-consumer count.
- **Proposals filed this run** — path + the gate outcome (`survived N/3`) for each `rebasing_proposed` that passed; and any that FAILED the gate (refutations named, not filed).
- **Pending proposals (all)** — every file under `proposals/`, so nothing sits unratified and unseen.
- **Ratification instructions** — the exact `ratify <name>@<version>` command per pending proposal.

---

## Important Notes

- **Auto-apply ONLY for pre-registered on_trip consequences.** A falsifier trip may fire its pre-registered `flag+disclose`-class consequence automatically; a parameter re-base never auto-applies. The two are different mechanisms — do not conflate a monitored trip with a ratified re-base.
- **Self-reported confidence is not a gate; refutation survival is.** A proposal you feel certain about but that a refutation pass breaks is NOT filed. The adversary, not the author, decides.
- **Forward-only versioning.** Never edit a scale in place. A re-base is a new version with the old one as its `prior`; the old version stays active and, on ratification, is archived with history retained.
- **The report footer always shows the active scale.** Every report a consumer renders shows `scale: <name>@<version>` for the scale in force, so an unratified proposal is always visible as *pending* against the active version — the tuning is never invisible.
- **Refresh monitoring vs. scale review.** `refresh_plan.py` monitors falsifiers on every refresh (cheap, automatic, per-ticker) and sets `scale_review_required`; THIS skill is the deliberate periodic re-examination that can propose a re-base. The refresh signals; the review decides.
- **No active scales → nothing to review.** If `trading_desk_config/scales/` is empty, say so and stop; there is nothing to re-examine.
- **Educational only.** This is analysis, not investment advice. A scale is a modeling anchor, not a price target — verify every parameter independently before acting.
