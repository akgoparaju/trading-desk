# `report-renderer --output-dir` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `report-renderer` reachable from a programmatic caller whose workspace is an absolute path outside the invoking CWD, so a complete-but-unfinished bundle can be finished without re-running the engine — and surface the report's word budget *before* the prose is authored.

**Architecture:** Two independent units. (1) `skills/report-renderer/SKILL.md` gains `--output-dir` / `--prev-dir` threading, ordered flat-then-legacy discovery, and a resume-by-default rule with a `--fresh-skeleton` override — **prose only, no script change**, because every script already accepts an absolute `--bundle` and walks up from it. (2) A new stdlib-only `scripts/word_budget.py` owns the word-counting primitives that `report_qc` already has; `report_qc` re-points at it via aliases (behavior untouched) and `render_report` uses it to print a budget block. One counter, so the render-time number and the gate's number cannot drift.

**Tech Stack:** Python 3.10+, stdlib only. pytest. Markdown SKILL files.

**Spec:** `docs/specs/2026-08-12-report-renderer-output-dir.md`
**Branch:** `feature/report-renderer-output-dir` (already created, spec already committed)
**Test command:** `python3 -m pytest tests/ -q` from `/Users/ankugo/dev/trading-desk`
**Baseline (verified before starting):** `2440 passed, 52 skipped, 18 subtests passed`

---

## File Structure

| File | Responsibility |
|---|---|
| `scripts/word_budget.py` | **NEW.** Sole owner of report word accounting: page splitting, countable-prose filtering, per-slot budgets, contributor ranking, and the printable block. stdlib-only, no I/O. |
| `scripts/report_qc.py` | Loses the two counting functions and their three regexes; keeps `_page_sections` / `_countable_prose` / `_WORD_CAP` / `_WORD_TRIM_MARGIN` as aliases so its behavior, messages and existing tests are unchanged. |
| `scripts/render_report.py` | Gains one import and two lines in `main()` — prints the budget block before the path line, full reports only. |
| `skills/report-renderer/SKILL.md` | Workspace-root resolution, ordered discovery with three guardrails, resume-by-default + `--fresh-skeleton`, drift rules, absolute-path fan-out, and the missing `--doc delta` command. |
| `tests/test_word_budget.py` | **NEW.** Unit tests for the new module, including parity against `report_qc`'s pre-existing numbers. |
| `tests/test_output_dir_workspace.py` | Gains doc-contract tests over `report-renderer/SKILL.md` — discovery is prose, so the document is the implementation. |

**Import direction matters:** `report_qc` already imports `render_report`, so `render_report` must never import `report_qc`. `word_budget` imports nothing from either. This is why the shared module exists rather than a cross-import.

---

## Task 1: `scripts/word_budget.py`

**Files:**
- Create: `scripts/word_budget.py`
- Test: `tests/test_word_budget.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_word_budget.py`:

```python
"""Tests for the shared report word-budget accounting.

WHY THIS MODULE EXISTS: measured on the 2026-08-12 PLTR bundle, the fresh
skeleton was 397 words against a 2100 cap with 14 open slots -- 1663 words of
room. The author spent 1736 and failed ``word_cap`` by 33, costing a trim plus a
full QC cycle at the most fragile point of the run. Nothing in the chain stated
the budget before the prose was written. These tests pin the accounting and,
critically, its PARITY with the gate that enforces it.

stdlib-only.
"""

from scripts import report_qc as rq
from scripts import word_budget as wb


REPORT = """# PLTR Trade Report

## Page 1 - Decision

### The Call

Constructive but sub-hurdle.

| metric | value |
|---|---|
| composite | 58.6 |

<!-- SLOT:tension -->

## Page 2 - Evidence

### Technical - 69.5/100

Trend intact above the rising 50-day.

<!-- SLOT:brief_technical -->
<!-- SLOT:signal_technical -->

## Page 3 - Context & Protocol

### Monitoring Protocol

Watch the print.

<!-- SLOT:monitoring_notes -->

### Data Integrity

This mandated footer is excluded from the count and must never be trimmed.
"""


# --------------------------------------------------------------------------- #
# Counting primitives
# --------------------------------------------------------------------------- #

def test_page_sections_returns_one_body_per_page():
    assert len(wb.page_sections(REPORT)) == 3


def test_countable_prose_drops_table_rows():
    body = wb.page_sections(REPORT)[0]
    assert "| composite | 58.6 |" not in wb.countable_prose(body)
    assert "Constructive but sub-hurdle." in wb.countable_prose(body)


def test_countable_prose_drops_the_data_integrity_footer():
    body = wb.page_sections(REPORT)[2]
    kept = wb.countable_prose(body)
    assert "mandated footer" not in kept
    assert "Watch the print." in kept


def test_count_words_returns_total_and_per_page():
    total, per_page = wb.count_words(REPORT)
    assert len(per_page) == 3
    assert total == sum(per_page)
    assert total > 0


# --------------------------------------------------------------------------- #
# PARITY -- the whole point of the shared module. The gate and the render-time
# block must report the SAME number for the same text, forever.
# --------------------------------------------------------------------------- #

def test_parity_with_report_qc_page_sections():
    assert wb.page_sections(REPORT) == rq._page_sections(REPORT)


def test_parity_with_report_qc_countable_prose():
    for body in wb.page_sections(REPORT):
        assert wb.countable_prose(body) == rq._countable_prose(body)


def test_parity_with_report_qc_constants():
    assert wb.WORD_CAP == rq._WORD_CAP == 2100
    assert wb.WORD_TRIM_MARGIN == rq._WORD_TRIM_MARGIN == 40


# --------------------------------------------------------------------------- #
# Slots and budgets
# --------------------------------------------------------------------------- #

def test_open_slots_are_returned_in_document_order():
    assert wb.open_slots(REPORT) == [
        "tension", "brief_technical", "signal_technical", "monitoring_notes"]


def test_open_slots_empty_when_every_slot_is_filled():
    assert wb.open_slots(REPORT.replace("<!-- SLOT:", "<!-- FILLED:")) == []


def test_slot_budget_by_family_and_by_name():
    assert wb.slot_budget("brief_fundamental") == 150
    assert wb.slot_budget("signal_fundamental") == 15
    assert wb.slot_budget("tension") == 30
    assert wb.slot_budget("event_playbook") == 60
    assert wb.slot_budget("catalyst_notes") == 35
    assert wb.slot_budget("monitoring_notes") == 35
    assert wb.slot_budget("delta_interpretation") == 60


def test_slot_budget_unknown_name_falls_back_without_raising():
    assert wb.slot_budget("some_future_slot") == 35


# --------------------------------------------------------------------------- #
# Contributor ranking -- over COUNTABLE text, so the excluded Data Integrity
# footer can never top the list. On the real fixture it is 1,192 words and would
# dominate every ranking if the raw text were split instead.
# --------------------------------------------------------------------------- #

def test_section_contributors_are_ranked_and_exclude_data_integrity():
    names = [name for name, _ in wb.section_contributors(REPORT, top_n=5)]
    assert "Data Integrity" not in names
    assert "The Call" in names


def test_section_contributors_strips_the_score_suffix_from_the_heading():
    names = [name for name, _ in wb.section_contributors(REPORT, top_n=5)]
    assert "Technical" in names, names


def test_section_contributors_respects_top_n():
    assert len(wb.section_contributors(REPORT, top_n=2)) == 2


# --------------------------------------------------------------------------- #
# The budget dict + block
# --------------------------------------------------------------------------- #

def test_budget_reports_room_as_cap_minus_margin_minus_skeleton():
    info = wb.budget(REPORT)
    assert info["room"] == wb.WORD_CAP - wb.WORD_TRIM_MARGIN - info["skeleton_words"]


def test_budget_sums_the_open_slot_budgets():
    info = wb.budget(REPORT)
    assert info["budgeted"] == 30 + 150 + 15 + 35


def test_budget_flags_open_brief_slots():
    assert wb.budget(REPORT)["brief_slots_open"] == ["brief_technical"]


def test_budget_not_oversubscribed_on_a_small_skeleton():
    assert wb.budget(REPORT)["oversubscribed"] is False


def test_budget_oversubscribed_when_budgets_exceed_the_room():
    fat = REPORT.replace("Constructive but sub-hurdle.", "word " * 2000)
    assert wb.budget(fat)["oversubscribed"] is True


def test_budget_flags_a_skeleton_that_is_already_near_cap():
    fat = REPORT.replace("Constructive but sub-hurdle.", "word " * 2100)
    assert wb.budget(fat)["skeleton_near_cap"] is True


def test_format_budget_block_names_every_open_slot_and_its_budget():
    block = wb.format_budget_block(wb.budget(REPORT))
    assert "WORD BUDGET" in block
    for name in ("tension", "brief_technical", "signal_technical",
                 "monitoring_notes"):
        assert name in block
    assert "150" in block


def test_format_budget_block_warns_when_brief_slots_are_open():
    block = wb.format_budget_block(wb.budget(REPORT))
    assert "brief slot" in block
    assert "transclude" in block


def test_format_budget_block_warns_when_oversubscribed_and_names_sections():
    fat = REPORT.replace("Constructive but sub-hurdle.", "word " * 2000)
    block = wb.format_budget_block(wb.budget(fat))
    assert "OVER-SUBSCRIBED" in block
    assert "The Call" in block


def test_format_budget_block_is_quiet_when_nothing_is_wrong():
    lean = REPORT.replace("<!-- SLOT:brief_technical -->", "filled")
    block = wb.format_budget_block(wb.budget(lean))
    assert "OVER-SUBSCRIBED" not in block
    assert "SKELETON NEAR CAP" not in block
    assert "brief slot" not in block
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_word_budget.py -q`
Expected: collection error — `ModuleNotFoundError: No module named 'scripts.word_budget'`

- [ ] **Step 3: Write the implementation**

Create `scripts/word_budget.py`:

```python
"""Shared word-budget accounting for the 3-page trade decision report.

ONE counter serves both ends of the authoring loop: ``render_report`` prints the
budget BEFORE a word is authored, and ``report_qc.check_word_cap`` enforces the
same number after. They cannot drift, because they call these functions.

WHY IT EXISTS: measured on the 2026-08-12 PLTR bundle, the fresh skeleton was 397
words against a 2100 cap with 14 open slots -- 1663 words of room. The author
spent 1736 and failed ``word_cap`` by 33 words. The trim cycle that failure forced
is where the run died. Nothing anywhere stated the budget before the prose was
written; this module is that statement.

WHY IT IS A SEPARATE MODULE: ``report_qc`` imports ``render_report``, so
``render_report`` can never import ``report_qc`` back. Both import this instead.

stdlib-only. No I/O.
"""

import re

# The cap the gate enforces and the overshoot margin its trim instruction uses.
# report_qc aliases these, so there is exactly one definition of each.
WORD_CAP = 2100
WORD_TRIM_MARGIN = 40

# Per-slot authoring budgets. MIRRORED in skills/report-renderer/SKILL.md's slot
# table -- change both together. The five briefs plus five signals plus the four
# small slots sum to 985, comfortably inside the room a normal skeleton leaves.
BRIEF_SLOT_BUDGET = 150
SIGNAL_SLOT_BUDGET = 15
_NAMED_SLOT_BUDGETS = {
    "tension": 30,
    "event_playbook": 60,
    "catalyst_notes": 35,
    "monitoring_notes": 35,
    "delta_interpretation": 60,
}
# A slot added upstream without a budget here still gets a sane number rather than
# a KeyError -- the block is a diagnostic and must never break a render.
_DEFAULT_SLOT_BUDGET = 35

_SLOT_RE = re.compile(r"<!--\s*SLOT:([a-z_]+)\s*-->")
_PAGE_RE = re.compile(r"^## Page ", re.MULTILINE)
_SUBSECTION_RE = re.compile(r"^### ", re.MULTILINE)

# A markdown pipe-table row (header, separator, or data): its stripped form starts
# with "|". Every one of these is SCRIPT-minted by render_report from the bundle --
# the author cannot shorten a table without deleting a scripted number.
_TABLE_ROW_RE = re.compile(r"^\|")
# Any ATX heading, with its level captured.
_HEADING_RE = re.compile(r"^(#{1,6})\s")
# The mandated disclosure footer's heading. Everything from here to the next
# heading of the SAME OR HIGHER level (or the end of the page) is the footer.
_DATA_INTEGRITY_RE = re.compile(r"^(#{1,6})\s+Data Integrity\s*$")


def page_sections(report_text):
    """Split the report into the Page-1/2/3 section bodies (list of strings).

    Splits on the ``## Page`` headers. Returns the section bodies (excluding any
    preamble before the first Page header).
    """
    parts = _PAGE_RE.split(report_text)
    # parts[0] is the preamble (title); the rest are the three pages.
    return parts[1:]


def countable_prose(section_text):
    """A page body with the UNCOUNTABLE parts removed, for the word cap.

    Two things are dropped:

      * every markdown pipe-table row -- the tables are minted by render_report
        from the bundle, so their words are not a budget the author can spend;
      * the whole ``### Data Integrity`` section (heading through to the next
        heading of the same or higher level, or the end of the page) -- mandated
        disclosure whose length is set by the snapshot's api_tier_notes, not by
        the author.

    WHY: measured on four production bundles the ZERO-PROSE skeleton alone ran
    2,816-3,935 words, so a cap of 2100 over the raw text was unsatisfiable by any
    amount of prose editing. The one way to "pass" was to delete scripted content
    -- and in production an author did exactly that, cutting 78% of the mandated
    footer. Counting only what an author can actually influence makes the cap a
    prose budget again and removes the incentive to shrink disclosure.
    """
    out = []
    skip_level = None
    for line in section_text.splitlines():
        stripped = line.strip()
        heading = _HEADING_RE.match(stripped)
        if heading:
            level = len(heading.group(1))
            if skip_level is not None and level <= skip_level:
                skip_level = None          # the footer section ended here
            if _DATA_INTEGRITY_RE.match(stripped):
                # Never DOWNGRADE an active skip to a deeper level: a delta report
                # nests "### Data Integrity" (the footer builder's own heading)
                # inside "## Data Integrity" (the page heading), and taking the
                # inner level would let the next "###" end the skip early. The
                # shallowest active heading owns the section.
                skip_level = level if skip_level is None else min(skip_level, level)
                continue
        if skip_level is not None:
            continue
        if _TABLE_ROW_RE.match(stripped):
            continue
        out.append(line)
    return "\n".join(out)


def count_words(report_text):
    """``(total, [p1, p2, p3])`` countable words across the page sections."""
    counts = [len(countable_prose(s).split()) for s in page_sections(report_text)]
    return sum(counts), counts


def open_slots(report_text):
    """Names of the slots still carrying an unfilled mark, in document order."""
    return _SLOT_RE.findall(report_text)


def slot_budget(name):
    """The recommended word budget for one slot.

    Families first (``brief_*``, ``signal_*``), then the named singletons, then a
    conservative default so an upstream slot this module has not heard of still
    gets a number.
    """
    if name.startswith("brief_"):
        return BRIEF_SLOT_BUDGET
    if name.startswith("signal_"):
        return SIGNAL_SLOT_BUDGET
    return _NAMED_SLOT_BUDGETS.get(name, _DEFAULT_SLOT_BUDGET)


def section_contributors(report_text, top_n=3):
    """Ranked ``(name, words)`` for the ``###`` subsections, largest first.

    Computed over COUNTABLE text, which is load-bearing: the ``### Data
    Integrity`` footer is 1,192 words on a real bundle and would top every
    ranking if the raw text were split instead -- pointing the author at the one
    section they must never trim.
    """
    rows = []
    for section in page_sections(report_text):
        for part in _SUBSECTION_RE.split(countable_prose(section))[1:]:
            lines = part.splitlines()
            if not lines:
                continue
            # Headings carry a scored suffix ("Technical - 69.5/100 (rubric ...)");
            # the leading token is the name a human recognises.
            name = re.split(r"\s+[-—]\s+", lines[0])[0].strip()
            rows.append((name, len("\n".join(lines[1:]).split())))
    rows.sort(key=lambda row: (-row[1], row[0]))
    return rows[:top_n]


def budget(report_text):
    """Everything the render-time block needs, as a plain dict."""
    skeleton_words, per_page = count_words(report_text)
    slots = open_slots(report_text)
    budgeted = sum(slot_budget(s) for s in slots)
    room = WORD_CAP - WORD_TRIM_MARGIN - skeleton_words
    return {
        "skeleton_words": skeleton_words,
        "per_page": per_page,
        "cap": WORD_CAP,
        "margin": WORD_TRIM_MARGIN,
        "room": room,
        "open_slots": slots,
        "budgeted": budgeted,
        "oversubscribed": budgeted > room,
        "skeleton_near_cap": skeleton_words > WORD_CAP - WORD_TRIM_MARGIN,
        "brief_slots_open": [s for s in slots if s.startswith("brief_")],
        "contributors": section_contributors(report_text, top_n=3),
    }


def format_budget_block(info):
    """The printable block. ASCII only -- this goes to a terminal."""
    cap, margin = info["cap"], info["margin"]
    skeleton = info["skeleton_words"]
    lines = [
        f"WORD BUDGET  skeleton {skeleton} / cap {cap}   "
        f"(prose+headings; table rows and Data Integrity excluded)",
        f"  room for authored prose: {info['room']}   "
        f"(cap {cap} - margin {margin} - skeleton {skeleton})",
    ]
    slots = info["open_slots"]
    if slots:
        lines.append(f"  {len(slots)} open slots, budgeted {info['budgeted']}:")
        for name in slots:
            lines.append(f"    {name:<24} {slot_budget(name)}")
    else:
        lines.append("  0 open slots -- every slot is filled or transcluded.")

    contrib = ", ".join(f"{n} {c}" for n, c in info["contributors"])
    if info["oversubscribed"]:
        lines.append(
            f"  !! OVER-SUBSCRIBED -- slot budgets ({info['budgeted']}) exceed the "
            f"room ({info['room']}). Writing to budget still fails the cap, so the "
            f"fix is upstream, not a tighter slot. Largest sections: {contrib}")
    if info["skeleton_near_cap"]:
        lines.append(
            f"  !! SKELETON NEAR CAP -- {skeleton} of {cap} before a word of prose. "
            f"Largest sections: {contrib}")
    if info["brief_slots_open"]:
        n = len(info["brief_slots_open"])
        lines.append(
            f"  NOTE  {n} brief slot(s) OPEN -- brief_*.md absent or missing its "
            f"delimiters, so ~{n * BRIEF_SLOT_BUDGET} words that normally "
            f"transclude must be authored here.")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_word_budget.py -q`
Expected: all pass (24 tests)

- [ ] **Step 5: Commit**

```bash
cd /Users/ankugo/dev/trading-desk
git add scripts/word_budget.py tests/test_word_budget.py
git commit -m "word_budget: shared report word accounting with per-slot budgets"
```

---

## Task 2: Re-point `report_qc` at the shared module

The counting functions now live in two places. Delete the copies in `report_qc` and alias, so there is exactly one definition and `check_word_cap`'s behavior, messages, and tests are all unchanged.

**Files:**
- Modify: `scripts/report_qc.py` (constants at ~76-80; the section at ~500-570)

- [ ] **Step 1: Confirm the current suite is green before touching it**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_report_qc.py -q`
Expected: all pass. Record the count.

- [ ] **Step 2: Add the import**

In `scripts/report_qc.py`, extend the existing import line (currently line 71):

```python
from scripts import render_report, chain as chain_mod, decision_contract, ev_kelly
from scripts import word_budget
```

- [ ] **Step 3: Alias the constants**

Replace the two constant definitions (lines ~76-80):

```python
_WORD_CAP = 2100
# Overshoot the trim by this much rather than converging onto the cap exactly. The
# measured tail on a real run was 2103 -> 2101 -> 2100, three QC cycles to recover
# three words. This changes only the TRIM INSTRUCTION, never the cap that is enforced.
_WORD_TRIM_MARGIN = 40
```

with:

```python
# The cap and the trim margin now live in word_budget, so render_report can print
# the SAME budget before the prose is authored that this gate enforces after.
# Aliased rather than re-declared: one definition, no drift.
_WORD_CAP = word_budget.WORD_CAP
# Overshoot the trim by this much rather than converging onto the cap exactly. The
# measured tail on a real run was 2103 -> 2101 -> 2100, three QC cycles to recover
# three words. This changes only the TRIM INSTRUCTION, never the cap that is enforced.
_WORD_TRIM_MARGIN = word_budget.WORD_TRIM_MARGIN
```

- [ ] **Step 4: Replace the moved section**

Delete everything from the `# Report section splitting.` banner comment (line ~500) through the end of `_countable_prose` (line ~569) — that is the banner, `_page_sections`, `_TABLE_ROW_RE`, `_HEADING_RE`, `_DATA_INTEGRITY_RE`, and `_countable_prose`. Those three regexes are used **only** by the two moved functions (verified: no other reference in the file). Replace the whole span with:

```python
# --------------------------------------------------------------------------- #
# Report section splitting -- MOVED to scripts/word_budget.py so render_report
# can count the skeleton with the same code this gate enforces with. Aliased
# under the old private names: callers and tests inside this module are
# unchanged, and there is exactly one implementation.
# --------------------------------------------------------------------------- #

_page_sections = word_budget.page_sections
_countable_prose = word_budget.countable_prose
```

- [ ] **Step 5: Run the report_qc tests**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_report_qc.py tests/test_word_budget.py -q`
Expected: all pass, same count as Step 1 plus the 24 new ones. The tests that call `rq._countable_prose`, `rq._page_sections`, `rq._WORD_CAP` and `rq._WORD_TRIM_MARGIN` must pass **unmodified** — if any needs editing, the alias is wrong; fix the alias, not the test.

- [ ] **Step 6: Run the full suite**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/ -q`
Expected: `2464 passed, 52 skipped, 18 subtests passed` (baseline 2440 + 24 new)

- [ ] **Step 7: Commit**

```bash
cd /Users/ankugo/dev/trading-desk
git add scripts/report_qc.py
git commit -m "report_qc: alias the word counters onto word_budget (one definition)"
```

---

## Task 3: Print the budget block from `render_report`

**Files:**
- Modify: `scripts/render_report.py` (import block ~46; `main()` full-report branch, the tail)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_word_budget.py`:

```python
# --------------------------------------------------------------------------- #
# render_report prints the block -- BEFORE the path, so the path stays the last
# line of stdout for anything that reads it that way (the SKILL does).
# --------------------------------------------------------------------------- #

def test_render_report_prints_the_budget_block_before_the_path(tmp_path, capsys,
                                                               monkeypatch):
    from scripts import render_report

    bundle = tmp_path / "detail_reports_2026-08-12"
    bundle.mkdir()
    out = tmp_path / "T_Trade_Report_2026-08-12.md"

    monkeypatch.setattr(render_report, "_require_full_modules", lambda b: None)
    monkeypatch.setattr(render_report, "load_bundle",
                        lambda b: {"snapshot": {"meta": {"ticker": "T",
                                                         "as_of_utc": "2026-08-12"}}})
    monkeypatch.setattr(render_report, "build_full_report",
                        lambda docs, bundle=None: REPORT)
    monkeypatch.setattr(render_report.decision_contract, "build_contract",
                        lambda docs: {"ok": True})

    rc = render_report.main(["--bundle", str(bundle), "--out", str(out)])

    assert rc == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert "WORD BUDGET" in "\n".join(lines)
    assert lines[-1] == str(out), "the report path must remain the LAST stdout line"
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_word_budget.py -q -k budget_block_before`
Expected: FAIL — `assert 'WORD BUDGET' in ...` (the block is not printed yet)

- [ ] **Step 3: Add the import**

In `scripts/render_report.py`, after line 47:

```python
from scripts import decision_contract  # noqa: E402  (after sys.path setup)
from scripts._artifact import emit_json  # noqa: E402  (after sys.path setup)
from scripts import word_budget  # noqa: E402  (after sys.path setup)
```

- [ ] **Step 4: Print the block**

In `main()`, in the full-report branch only (NOT the `--delta` branch — `check_word_cap` does not run on deltas), replace:

```python
    contract = decision_contract.build_contract(docs)
    decision_path = os.path.join(args.bundle, "module_decision.json")
    emit_json(contract, decision_path)

    print(out)
    return 0
```

with:

```python
    contract = decision_contract.build_contract(docs)
    decision_path = os.path.join(args.bundle, "module_decision.json")
    emit_json(contract, decision_path)

    # The authoring budget, stated BEFORE a word of prose is written -- the gate
    # states it after, and by then an overage costs a trim plus a full QC cycle.
    # Printed before the path so the path stays the LAST line of stdout.
    print(word_budget.format_budget_block(word_budget.budget(report)))

    print(out)
    return 0
```

- [ ] **Step 5: Run the test**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_word_budget.py -q`
Expected: all pass (25 tests)

- [ ] **Step 6: Verify against the real fixture**

```bash
cd /Users/ankugo/dev/trading-desk
SCRATCH=/private/tmp/claude-501/-Users-ankugo-dev-jutsu-trading-desk/b1c4bf68-9c9e-47f0-b82b-07886f632457/scratchpad/acceptance
python3 scripts/render_report.py --bundle "$SCRATCH/PLTR_2026-08-12/detail_reports_2026-08-12"
```

Expected: a block reporting `skeleton 397 / cap 2100`, `room for authored prose: 1663`, `14 open slots, budgeted 985`, the `NOTE 5 brief slot(s) OPEN` line, and NO over-subscribed or near-cap warning. The last line is the report path.

- [ ] **Step 7: Run the full suite and commit**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/ -q`
Expected: `2465 passed, 52 skipped, 18 subtests passed`

```bash
cd /Users/ankugo/dev/trading-desk
git add scripts/render_report.py tests/test_word_budget.py
git commit -m "render_report: print the authoring word budget before the path"
```

---

## Task 4: SKILL.md — workspace root and ordered discovery

**Files:**
- Modify: `skills/report-renderer/SKILL.md` (the `**Output location:**` paragraph area, and Step 1)

- [ ] **Step 1: Insert the workspace-root section**

In `skills/report-renderer/SKILL.md`, immediately **after** the `**Output location:** …` paragraph (the one ending "`--out` overrides.") and **before** the `**Why this architecture …**` paragraph, insert:

```markdown
**Workspace root (`--output-dir`) + prior workspace (`--prev-dir`).** This skill accepts an optional **`--output-dir <ABS_DIR>`** and, for a delta note, an optional **`--prev-dir <ABS_DIR>`** (e.g. `report-renderer PLTR --output-dir /abs/workspace --prev-dir /abs/prior`). Resolve them FIRST:

- **`--output-dir <ABS_DIR>` given** → `WORKROOT = <ABS_DIR>` (MUST be absolute) and — **FLAT under `--output-dir` (v1.2.0)** — `TICKER_WS = <WORKROOT>`: the ticker workspace IS `<WORKROOT>`, so drop the `trading_desk_<TICKER>/` segment (the caller passes a per-ticker dir). All I/O is rooted here, decoupled from the process CWD.
- **`--output-dir` absent** → `WORKROOT = .` and `TICKER_WS = ./trading_desk_<TICKER>` (the human/CWD layout — byte-for-byte unchanged).
- **`--prev-dir <ABS_DIR>`** (delta only) → the PRIOR **workspace root**; `<PREV_BUNDLE>` = the newest `detail_reports_*` under it. **Tolerant:** if the given path's own basename starts with `detail_reports`, it IS `<PREV_BUNDLE>` — a caller who handed you a bundle instead of a root is right, not wrong. **`--prev-dir` is READ-ONLY**; never write into it.

**One name per layer, translated exactly once.** The SKILL boundary takes `--prev-dir <workspace root>`; the SCRIPT boundary takes `--previous <bundle dir>`. Resolve `--prev-dir` → `<PREV_BUNDLE>` here, once, then pass the **bundle** to every `render_report.py --previous`, `report_qc.py --previous`, and `render_pdf.py --previous` below.

**Fan it out.** Give every `python3 scripts/…` path argument (`--bundle`, `--report`, `--previous`, `--pdf-slots`, `--out`) as an **absolute path** built from `<BUNDLE>` / `<TICKER_WS>`. The scripts then never fall back to the CWD: `render_report.py` writes the report to the bundle's parent per the output-location rule above, and config/scale discovery walks up from the absolute bundle path.
```

- [ ] **Step 2: Replace Step 1 entirely**

Replace the whole `## Step 1 — Verify bundle completeness` section (from its heading through the paragraph ending "the **files** must exist.") with:

```markdown
## Step 1 — Locate the bundle, then verify completeness

**Discovery is ORDERED, and its result is TERMINAL.** Take the first branch that matches, then stop looking:

**(a) `--output-dir` given — flat (v1.2.0), the programmatic caller's layout:**
```bash
ls -dt <WORKROOT>/detail_reports_* 2>/dev/null | head -1
```

**(b) ONLY if (a) matched nothing — a pre-v1.2.0 nested workspace, or a legacy bundle, sitting under the given root:**
```bash
ls -dt <WORKROOT>/trading_desk_<TICKER>/detail_reports_* <WORKROOT>/td_bundle_<TICKER>_* 2>/dev/null | head -1
```

**(c) `--output-dir` absent — the invoker's CWD (unchanged):**
```bash
ls -dt ./trading_desk_<TICKER>/detail_reports_* ./td_bundle_<TICKER>_* 2>/dev/null | head -1
```

Keep (a) and (b) as SEPARATE commands in that order — **never merge them into one `ls -dt`**. A merged glob sorts by mtime across layouts, so a stale legacy sibling can outrank the flat bundle the caller meant and you would silently render the wrong bundle. Flat is authoritative; (b) is last resort.

**ANNOUNCE which branch resolved**, with the path: `bundle: <BUNDLE> (discovery: flat under --output-dir | legacy nested fallback under --output-dir | CWD)`. A fallback that fires silently is indistinguishable from one that never fired, and on a recovery path the operator needs to know which bundle was actually rendered.

**If nothing matched, do NOT assert absence.** Say `no bundle found at <WORKROOT> (searched detail_reports_*, then trading_desk_<TICKER>/detail_reports_* and td_bundle_<TICKER>_*)`. A denied `readdir` returns EMPTY rather than erroring, so `ls` cannot tell "nothing here" from "I cannot read this directory". When `<WORKROOT>` is outside the home volume, add: verify readability with a **real byte-read** (e.g. `head -c 1 <WORKROOT>/trading_desk_config.json`) — **`stat` succeeds against a dead volume** and proves nothing.

**Completeness is a SEPARATE, LATER gate — never a reason to resume discovery.** Once a bundle resolves, that IS the bundle. A **full report requires all seven module files plus a snapshot**: `module_technical`, `module_risk`, `module_sentiment`, `module_fundamental`, `module_composite`, `module_tradeplan`, `module_options`. If any is missing, the renderer exits 2 naming it — report that and **STOP**. **Never fall back to a different bundle because this one is incomplete**; run the missing upstream skill first (composite-score runs the four evidence skills; trade-plan runs composite then options-strategy; then synthesize). Renormalized absences *inside* a module are fine; the **files** must exist.
```

- [ ] **Step 3: Verify the guardrail tokens are present**

```bash
cd /Users/ankugo/dev/trading-desk
grep -c -- "--output-dir" skills/report-renderer/SKILL.md
grep -c -- "--prev-dir" skills/report-renderer/SKILL.md
grep -n "stat\` succeeds against a dead volume" skills/report-renderer/SKILL.md
grep -n "never a reason to resume discovery" skills/report-renderer/SKILL.md
```

Expected: `--output-dir` count ≥ 6, `--prev-dir` count ≥ 4, and both `grep -n` calls print a line.

- [ ] **Step 4: Commit**

```bash
cd /Users/ankugo/dev/trading-desk
git add skills/report-renderer/SKILL.md
git commit -m "report-renderer: --output-dir/--prev-dir threading and ordered discovery"
```

---

## Task 5: SKILL.md — resume by default, `--fresh-skeleton` to force

**Files:**
- Modify: `skills/report-renderer/SKILL.md` (Step 2, and Step 4's fix loop)

- [ ] **Step 1: Replace the Step 2 heading and its command block**

Replace:

```markdown
## Step 2 — Render the skeleton

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle ./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>
```

The script writes `<TICKER>_Trade_Report_<date>.md` (exact path — bundle or parent per the output-location rule above — printed to stdout). It contains three pages, all tables and numbers already filled from the bundle, with empty `<!-- SLOT:name -->` marks for your prose:
```

with:

```markdown
## Step 2 — Render the skeleton (SKIPPED when a report already exists)

**Check for an existing report FIRST.** It lives at `<REPORT> = <TICKER_WS>/<TICKER>_Trade_Report_<date>.md` (the bundle's parent under the `detail_reports_<date>/` layout; inside the bundle for legacy).

- **`<REPORT>` exists → SKIP the render.** Announce it loudly: `resumed existing report at <REPORT>; skeleton not re-rendered.` Go straight to Step 3 (fill any open slots) and Step 4 (QC). **Touch NOTHING in the bundle on this path.** This is the recovery entry point — a bundle can be complete and QC-passing while the report is a trim away from shipping, and re-rendering would discard every authored word and rewrite `module_decision.json` for nothing.
- **`<REPORT>` absent → render it:**

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/render_report.py --bundle <BUNDLE>
```

- **`--fresh-skeleton` given → render even though `<REPORT>` exists**, discarding the authored prose. State plainly that you are doing so and what is being discarded. **Never choose this branch on your own initiative**: throwing away authored analysis is an operator's call.

A fresh render also (re)writes `<BUNDLE>/module_decision.json` — that is Step 2's job and is expected here. It must not happen on the resume path, which is exactly why the resume path does not render.

The script writes `<TICKER>_Trade_Report_<date>.md` (exact path — bundle or parent per the output-location rule above — printed to stdout as the LAST line) and, before it, a **WORD BUDGET** block: the skeleton's own word count, the room left under the cap, and a per-slot budget for every open slot. **Read it before you author** — it is the only place the budget appears before the prose is written. `!! OVER-SUBSCRIBED` means writing to budget still fails the cap and the fix is upstream, not a tighter slot. `NOTE n brief slot(s) OPEN` means the `brief_<dim>.md` transclusion did not happen, so ~150 words per open brief that normally arrive for free must be authored here.

The report contains three pages, all tables and numbers already filled from the bundle, with empty `<!-- SLOT:name -->` marks for your prose:
```

- [ ] **Step 2: Add the drift rules to Step 4's fix loop**

In `## Step 4 — Run the blocking §12 QC gate`, append two bullets to the end of the **Fix loop** list (after the `A **table**-driven check fails …` bullet):

```markdown
- **On a RESUMED report, a numeric/structural failure means DRIFT.** If `number_provenance`, `composite_arithmetic`, `ev_consistency`, `sizing_within_cap`, `exit_ordering`, `strikes_in_chain`, `pop_method_labeled` or `expression_consistency` fails on a report you **resumed** rather than rendered, the report no longer matches the bundle. **STOP and report it, naming the remedy:** re-run with `--fresh-skeleton` to discard the prose and render a current skeleton. **Never auto-re-render** — that silently destroys authored analysis, and the choice belongs to the operator.
- **`word_cap` and `no_empty_slots` are NOT drift.** An over-cap resumed report gets trimmed; one with open slots gets them filled. Neither is a reason to re-render.
```

- [ ] **Step 3: Verify**

```bash
cd /Users/ankugo/dev/trading-desk
grep -n "resumed existing report at" skills/report-renderer/SKILL.md
grep -c -- "--fresh-skeleton" skills/report-renderer/SKILL.md
```

Expected: the first prints a line; the second is ≥ 3.

- [ ] **Step 4: Commit**

```bash
cd /Users/ankugo/dev/trading-desk
git add skills/report-renderer/SKILL.md
git commit -m "report-renderer: resume an existing report by default, --fresh-skeleton to force"
```

---

## Task 6: SKILL.md — absolute-path fan-out and the missing `--doc delta`

**Files:**
- Modify: `skills/report-renderer/SKILL.md` (Steps 4, 4b, 5b, 5d, 5e; Delta mode; Important Notes)

- [ ] **Step 1: Replace every hardcoded bundle path**

Every remaining occurrence of `./trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>` in the file becomes `<BUNDLE>`. There are occurrences in Step 4, Step 4b, Step 5(b), Step 5(d) (twice — `--bundle` and `--pdf-slots`), and Step 5(e) (twice). Verify none remain:

```bash
cd /Users/ankugo/dev/trading-desk
grep -c "trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>" skills/report-renderer/SKILL.md
```
Expected: `0`

In Step 4, also change `--report <path printed by render_report.py, e.g. ./<TICKER>_Trade_Report_<date>.md>` to `--report <REPORT>`.

- [ ] **Step 2: Make the pdf_slots gate carry `--previous`**

Replace the Step 5(d) command:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py \
  --bundle <BUNDLE> --pdf-slots <BUNDLE>/pdf_slots.json
```

with:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/report_qc.py \
  --bundle <BUNDLE> --pdf-slots <BUNDLE>/pdf_slots.json [--previous <PREV_BUNDLE>]
```

and add after the existing "Exit 0 = pass (stamp written)…" sentence:

```markdown
Pass **`--previous <PREV_BUNDLE>`** whenever a prior bundle resolved — it admits the Δ values `delta_interpretation` cites into the allowed provenance set. Without it, legitimate delta numbers orphan.
```

- [ ] **Step 3: Add `delta_interpretation` to the Step 5(c) rules**

Replace the `delta_interpretation` bullet in Step 5(c):

```markdown
- `delta_interpretation` — **null** for exec/detail (it belongs to the delta note; the refresh-analysis skill fills it).
```

with:

```markdown
- `delta_interpretation` — **REQUIRED (1-2 sentences) whenever a prior bundle resolved** (`--prev-dir` given, or a prior sits beside the bundle): what drove the composite / EV / level moves, citing ONLY numbers in the delta report or the module JSONs. **`null` when there is no prior** — then the docket is exec + detail only, which is a normal two-PDF pass, not a degradation to disclose.
```

- [ ] **Step 4: Add the delta PDF command to Step 5(e)**

Replace the Step 5(e) command block:

```bash
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc exec \
  [--previous <previous_bundle>]
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc detail
```

with:

```bash
# delta FIRST when a prior resolved -- it REQUIRES --previous
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc delta --previous <PREV_BUNDLE>
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc exec [--previous <PREV_BUNDLE>]
<venv-python> ${CLAUDE_PLUGIN_ROOT}/scripts/render_pdf.py \
  --bundle <BUNDLE> --doc detail
```

and replace the sentence beginning "The PDFs land in the **ticker parent**…" with:

```markdown
The PDFs land in the **ticker parent** (same location rule as the md report): `<TICKER>_Trade_Report_<date>.pdf` (exec), `<TICKER>_Detail_<date>.pdf` (detail), and `<TICKER>_Delta_Note_<date>.pdf` (delta), path printed to stdout. **The delta note is conditional on a resolvable prior bundle** — with no prior, a two-PDF docket is a complete pass. `render_pdf` exits 3 with a bootstrap line if the venv disappeared between steps — treat that exactly like the (a) exit-3 md-only fallback.
```

- [ ] **Step 5: Update Delta mode's commands**

In `## Delta mode`, replace `--bundle ./<new_bundle> --delta --previous ./<old_bundle>` with `--bundle <BUNDLE> --delta --previous <PREV_BUNDLE>`, and in the QC command replace `--bundle ./<new_bundle>` / `--previous ./<old_bundle>` with `--bundle <BUNDLE>` / `--previous <PREV_BUNDLE>`.

- [ ] **Step 6: Update the Important Notes**

Replace the final bullet:

```markdown
- **Read-only over the bundle (except the two authored artifacts).** This skill writes the report `.md` and — for the docket — `pdf_slots.json` + the `charts/` PNGs + the PDFs; it never edits the snapshot or any module JSON.
```

with:

```markdown
- **Read-only over the bundle (except the authored artifacts).** This skill writes the report `.md` and — for the docket — `pdf_slots.json` + the `charts/` PNGs + the PDFs. It **never** edits the snapshot or any evidence module JSON. `module_decision.json` is written by `render_report.py` on a **fresh render only** (Step 2); on the **resume** path nothing in the bundle is touched at all. `<PREV_BUNDLE>` is read-only in every mode.
- **No implicit re-analysis.** A missing module file exits 2 naming it. Never invoke an upstream skill to fill the gap — that turns a cheap recovery into an unaudited partial re-run.
```

- [ ] **Step 7: Verify and commit**

```bash
cd /Users/ankugo/dev/trading-desk
grep -c -- "--doc delta" skills/report-renderer/SKILL.md   # expect >= 1
grep -c "<BUNDLE>" skills/report-renderer/SKILL.md          # expect >= 10
git add skills/report-renderer/SKILL.md
git commit -m "report-renderer: absolute-path fan-out and the missing --doc delta command"
```

---

## Task 7: Doc-contract tests

Discovery is skill prose, not code — the document IS the implementation, so asserting on it is the only available regression test.

**Files:**
- Modify: `tests/test_output_dir_workspace.py` (append)

- [ ] **Step 1: Write the tests**

Append to `tests/test_output_dir_workspace.py`:

```python
# --------------------------------------------------------------------------- #
# 1.6.0 -- report-renderer workspace-root threading. Its Step-1 discovery is
# SKILL PROSE, not code: the LLM runs the globs. The document is therefore the
# implementation, and these are its regression tests. They also guard the
# stale-doc-string class that 1.5.1 made a standing release-procedure sweep.
# --------------------------------------------------------------------------- #

_SKILL_MD = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "skills", "report-renderer", "SKILL.md")


def _renderer_skill():
    with open(_SKILL_MD) as fh:
        return fh.read()


def test_renderer_skill_documents_both_workspace_flags():
    text = _renderer_skill()
    assert "--output-dir" in text
    assert "--prev-dir" in text


def test_renderer_skill_discovery_is_flat_before_the_fallback():
    """D4: ORDERED, never merged -- a merged ls -dt lets a stale legacy sibling
    win on mtime, which is a silent wrong-bundle render."""
    text = _renderer_skill()
    flat = text.index("<WORKROOT>/detail_reports_*")
    nested = text.index("<WORKROOT>/trading_desk_<TICKER>/detail_reports_*")
    assert flat < nested


def test_renderer_skill_keeps_guardrail_1_discovery_is_terminal():
    """G1 (non-negotiable): an incomplete bundle exits 2, it does NOT fall
    through to a different bundle."""
    assert "never a reason to resume discovery" in _renderer_skill()


def test_renderer_skill_keeps_guardrail_2_announce_the_branch():
    assert "discovery: flat under --output-dir" in _renderer_skill()


def test_renderer_skill_keeps_guardrail_3_never_assert_absence():
    """G3: a denied readdir returns empty, so 'not found' must not claim the
    directory is empty, and the remedy must be a byte-read, not stat."""
    text = _renderer_skill()
    assert "no bundle found at" in text
    assert "succeeds against a dead volume" in text


def test_renderer_skill_resumes_an_existing_report_by_default():
    """D-R: never silently re-render -- it discards authored prose."""
    text = _renderer_skill()
    assert "resumed existing report at" in text
    assert "--fresh-skeleton" in text


def test_renderer_skill_can_render_the_delta_note():
    """D7: Step 5 has always promised a three-PDF docket."""
    assert "--doc delta" in _renderer_skill()


def test_renderer_skill_has_no_cwd_relative_bundle_literals_left():
    """Every scripted path is absolute from <BUNDLE>; a stray CWD literal would
    silently reintroduce the bug this release exists to fix."""
    assert "trading_desk_<TICKER>/detail_reports_<YYYY-MM-DD>" not in _renderer_skill()
```

- [ ] **Step 2: Run them**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/test_output_dir_workspace.py -q`
Expected: all pass. If `os` is not already imported at the top of that file, add `import os`.

- [ ] **Step 3: Full suite and commit**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/ -q`
Expected: `2473 passed, 52 skipped, 18 subtests passed`

```bash
cd /Users/ankugo/dev/trading-desk
git add tests/test_output_dir_workspace.py
git commit -m "tests: doc-contract for report-renderer discovery, guardrails and resume"
```

---

## Task 8: Acceptance on the copied fixtures

**Never write to `/Volumes/OWC-2TB/dev/kurama-data/…`.** The copies are already at
`$SCRATCH = /private/tmp/claude-501/-Users-ankugo-dev-jutsu-trading-desk/b1c4bf68-9c9e-47f0-b82b-07886f632457/scratchpad/acceptance`
(`PLTR_2026-08-12`, `PLTR_2026-08-11`), with the live baseline in `live_mtimes_before.txt`.

Note: `PLTR_2026-08-12/PLTR_Trade_Report_2026-08-12.md` was overwritten with a skeleton during spec measurement. Restore the authored original first — the resume path is what this test exercises.

- [ ] **Step 1: Restore the fixture to its as-died state**

```bash
SCRATCH=/private/tmp/claude-501/-Users-ankugo-dev-jutsu-trading-desk/b1c4bf68-9c9e-47f0-b82b-07886f632457/scratchpad/acceptance
cp "$SCRATCH/authored_report_ORIG.md" "$SCRATCH/PLTR_2026-08-12/PLTR_Trade_Report_2026-08-12.md"
cp "$SCRATCH/module_decision_ORIG.json" "$SCRATCH/PLTR_2026-08-12/detail_reports_2026-08-12/module_decision.json"
find "$SCRATCH/PLTR_2026-08-12/detail_reports_2026-08-12" -exec stat -f '%m %N' {} \; | sort > "$SCRATCH/bundle_mtimes_before.txt"
```

- [ ] **Step 2: Copy the ORCL nested fixture for the fallback case**

```bash
SCRATCH=/private/tmp/claude-501/-Users-ankugo-dev-jutsu-trading-desk/b1c4bf68-9c9e-47f0-b82b-07886f632457/scratchpad/acceptance
cp -RL /Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/stock-analysis/ORCL/2026-07-23 "$SCRATCH/ORCL_2026-07-23"
ls "$SCRATCH/ORCL_2026-07-23/trading_desk_ORCL/"
```
Expected: two nested bundles, `detail_reports_2026-07-23` and `detail_reports_2026-07-24`.

- [ ] **Step 3: Run the skill on the PLTR fixture, from the plugin repo root**

Invoke the **skill** (not the scripts directly — the skill boundary is what is being tested):

```
trading-desk:report-renderer PLTR --output-dir <SCRATCH>/PLTR_2026-08-12 --prev-dir <SCRATCH>/PLTR_2026-08-11
```

Follow the SKILL as written. Expected path through it: discovery announces `flat under --output-dir`; Step 2 announces `resumed existing report`; Step 4 fails `word_cap` at 2133/2100 and demands ≥73 words in one edit; trim ≥73 words from the *authored prose* (never a table row, never the Data Integrity footer); re-run to green; Step 4b passes; Step 5 renders charts, `pdf_slots.json` (with `delta_interpretation`, since a prior resolved), the slots gate with `--previous`, then the three PDFs.

- [ ] **Step 4: Verify the pass criteria**

```bash
SCRATCH=/private/tmp/claude-501/-Users-ankugo-dev-jutsu-trading-desk/b1c4bf68-9c9e-47f0-b82b-07886f632457/scratchpad/acceptance
# 3 -- three non-empty PDFs in the workroot
ls -l "$SCRATCH/PLTR_2026-08-12/"*.pdf
# 4 -- nothing in the bundle mutated except pdf_slots.json and charts/
find "$SCRATCH/PLTR_2026-08-12/detail_reports_2026-08-12" -exec stat -f '%m %N' {} \; | sort > "$SCRATCH/bundle_mtimes_after.txt"
diff "$SCRATCH/bundle_mtimes_before.txt" "$SCRATCH/bundle_mtimes_after.txt"
# 5 -- kurama's live workspace untouched
find /Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/stock-analysis/PLTR/2026-08-12 -exec stat -f '%m %N' {} \; | sort > "$SCRATCH/live_mtimes_after.txt"
diff "$SCRATCH/live_mtimes_before.txt" "$SCRATCH/live_mtimes_after.txt" && echo "LIVE WORKSPACE UNTOUCHED"
```

Expected: three non-empty PDFs; the bundle diff shows **only** `pdf_slots.json` and `charts/` entries — **`module_decision.json` must NOT appear**; the live diff is empty.

- [ ] **Step 5: Run the fallback case (discovery only)**

```
trading-desk:report-renderer ORCL --output-dir <SCRATCH>/ORCL_2026-07-23
```

Expected: flat matches nothing; the fallback resolves `<SCRATCH>/ORCL_2026-07-23/trading_desk_ORCL/detail_reports_2026-07-24` (newest wins *inside* the fallback); the run announces `legacy nested fallback under --output-dir`. Stop after discovery + the completeness check — this case is not carried to a docket.

- [ ] **Step 6: Record the results**

Write the observed outputs (discovery lines, the `word_cap` failure and the trim, the budget block, the three PDF paths and sizes, both diffs) into `docs/specs/2026-08-12-report-renderer-output-dir.md` under a new `## 7. Acceptance results` section. Report **what happened**, including anything that failed.

- [ ] **Step 7: Commit**

```bash
cd /Users/ankugo/dev/trading-desk
git add docs/specs/2026-08-12-report-renderer-output-dir.md
git commit -m "spec: record acceptance results for --output-dir recovery path"
```

---

## Task 9: Release 1.6.0

- [ ] **Step 1: Bump the version**

Edit `.claude-plugin/plugin.json`: `"version": "1.5.1"` → `"version": "1.6.0"`.

- [ ] **Step 2: Stale-version-string sweep** (a standing release-procedure step since 1.5.1)

```bash
cd /Users/ankugo/dev/trading-desk
grep -rn "v1\.[0-9]\.[0-9]" skills/*/SKILL.md | grep -v "v1.2.0" | head -40
python3 -c "
import sys; sys.path.insert(0,'.')
from scripts import score_technical, score_fundamental, score_sentiment, score_risk, score_composite, build_snapshot, trade_plan, valuation_reconcile
print('technical', score_technical.RUBRIC_VERSION)
print('fundamental', score_fundamental.RUBRIC_VERSION)
print('sentiment', score_sentiment.RUBRIC_VERSION)
print('risk', score_risk.RUBRIC_VERSION)
print('composite', score_composite.RUBRIC_VERSION)
print('snapshot', build_snapshot.SCHEMA_VERSION)
print('reconcile', valuation_reconcile.RECONCILE_VERSION)
"
```

Cross-check every `vX.Y.Z` in each SKILL.md against the shipped constant. A historically-accurate citation (e.g. "the FLAT layout shipped in v1.2.0") is correct and stays. Fix any genuine mismatch.

- [ ] **Step 3: Write the CHANGELOG entry**

Prepend to `CHANGELOG.md`, under the `# Changelog` heading, following the house style (a lead paragraph with the suite count, then bolded bullets that name measured numbers):

```markdown
## 1.6.0 — 2026-08-12 · `report-renderer` becomes reachable from a programmatic caller, and the report's word budget is stated before the prose is written

Two units over 1.5.1, both path-and-diagnostic work: **zero scored change, no gate relaxed, no number moved.** `report-renderer` was the only one of the three docket-producing skills a programmatic caller could not reach — its Step-1 discovery was CWD-relative — so any interruption *after* a bundle was complete cost a full engine re-run. Suite **2473 passed, 52 skipped** (1.5.1: 2440/52).

- **`--output-dir` / `--prev-dir` threading on `report-renderer`**, semantics identical to `full-trade-analysis` (v1.1.0) and `refresh-analysis` (v1.2.0): `WORKROOT` given ⇒ FLAT `TICKER_WS = <WORKROOT>`, all scripted paths absolute, CWD never consulted. **No script changed** — `render_report._output_dir`, `render_pdf._scale_workspace_root` and `score_composite._resolve_default_config` already walked up from an absolute `--bundle`; only the skill's discovery was bound to the CWD. **Discovery is flat-first with an ORDERED nested/legacy fallback, deliberately not merged into one `ls -dt`**: a merged glob sorts by mtime across layouts, so a stale legacy sibling could outrank the flat bundle and silently render the wrong one. Three guardrails: discovery success is terminal (an incomplete bundle exits 2 naming the module — it never falls through to a different bundle), the resolving branch is announced, and the not-found message never asserts absence (a denied `readdir` returns empty rather than erroring, so "nothing here" and "I cannot read this" are indistinguishable to `ls`; the suggested remedy is a real byte-read, since `stat` succeeds against a dead volume).
- **`--prev-dir`, not `--previous`, and translated exactly once.** At the script boundary `--previous` already means a *bundle* directory; reusing the token for a *workspace root* at the skill boundary would have been a wrong-path bug waiting six weeks to happen. The skill resolves `--prev-dir` → `<PREV_BUNDLE>` in one place and passes the bundle onward. Tolerant: a path whose basename starts with `detail_reports` is accepted as the bundle itself.
- **Resume by default; `--fresh-skeleton` to force.** Step 2 re-rendered unconditionally, which on a recovery discards the authored prose (**1,736 words across 14 slots** on the fixture that prompted this) and rewrites `module_decision.json`. Now: report present ⇒ QC it as-is and touch nothing in the bundle; report absent ⇒ render as before; `--fresh-skeleton` ⇒ re-render, explicitly. A numeric/structural QC failure on a *resumed* report is drift and **fails loudly naming the flag** rather than silently re-rendering — discarding authored analysis is an operator's decision. `word_cap` and `no_empty_slots` are explicitly not drift. Safe because `number_provenance` re-verifies every figure against the bundle, so a resumed report that passes QC carries the same guarantee as a fresh one.
- **`--doc delta` reaches `report-renderer` (latent doc gap, closed).** Step 5 has always promised "three deterministic PDFs (exec, detail, and, when a prior bundle exists, a delta note)" while issuing exactly two `render_pdf.py` calls — the delta command existed only in `refresh-analysis`. Invisible until a caller needed a docket from this skill. The delta stays conditional: with no prior, a two-PDF docket is a pass, not a degradation.
- **`scripts/word_budget.py` — the authoring budget, stated BEFORE the prose.** `render_report.py` now prints the skeleton's word count, the room left under the cap, and a per-slot budget for every open slot. **Measured on the bundle this came from: skeleton 397/2100 with 14 open slots ⇒ 1,663 words of room; the author spent 1,736 and failed `word_cap` by 33** — and the trim cycle that failure forced is when the run died. The block warns three ways: budgets over-subscribing the room, a skeleton already near cap, and — the one that matters most here — **open `brief_*` slots**, which mean the `brief_<dim>.md` transclusion did not happen and ~150 words per brief that normally arrive for free must be authored instead. On this fixture that single line makes the whole causal chain visible before a word is written.
- **One counter, by construction.** `report_qc` already imports `render_report`, so the reverse import is impossible; the counting primitives moved to `word_budget` and `report_qc` aliases them under their old private names. The number printed at render time and the number the gate enforces are now the same code — they cannot drift. `check_word_cap`'s behavior, messages, and tests are unchanged.

**Known limits, disclosed.** The budget block prints on every full render, redirected or not — stdout gains the block (the path stays the last line); no artifact changes. The resume path prints no budget block, because nothing is rendered; an over-cap resumed report still learns its overage from `word_cap`'s existing (and already good) failure message.
```

- [ ] **Step 4: Run the full suite**

Run: `cd /Users/ankugo/dev/trading-desk && python3 -m pytest tests/ -q`
Expected: `2473 passed, 52 skipped, 18 subtests passed`

- [ ] **Step 5: Commit, merge, tag**

```bash
cd /Users/ankugo/dev/trading-desk
git add .claude-plugin/plugin.json CHANGELOG.md skills/
git commit -m "release 1.6.0: report-renderer --output-dir recovery path + render-time word budget"
git checkout main
git merge --no-ff feature/report-renderer-output-dir -m "Merge feature/report-renderer-output-dir → 1.6.0"
git tag v1.6.0
git push origin main --tags
```

- [ ] **Step 6: Deploy and VERIFY in the resolved tree**

Merging and tagging is not the deploy — the plugin cache is keyed by version directory.

```bash
ls -d ~/.claude/plugins/cache/*/trading-desk/1.6.0 2>/dev/null || echo "NOT YET INSTALLED"
```

Install/refresh the plugin, then confirm the markers are actually in the resolved tree:

```bash
CACHE=$(ls -d ~/.claude/plugins/cache/*/trading-desk/1.6.0 | head -1)
grep -c -- "--output-dir" "$CACHE/skills/report-renderer/SKILL.md"   # expect >= 6
test -f "$CACHE/scripts/word_budget.py" && echo "word_budget deployed"
```

- [ ] **Step 7: Fill the handoff status block**

Edit `/Volumes/OWC-2TB/dev/kurama-data/handoff/trading-desk/2026-08-12-report-renderer-output-dir.md` — **only** the status block at the top (this file is the agreed channel; the rest of the doc is the requester's):

```
Status: DONE
Implemented flag name(s):  --output-dir <ABS_DIR> | --prev-dir <ABS_DIR> | --fresh-skeleton
Discovery behavior:        ORDERED. (a) <WORKROOT>/detail_reports_* (flat, v1.2.0, authoritative);
                           (b) ONLY if (a) is empty: <WORKROOT>/trading_desk_<T>/detail_reports_*
                           then <WORKROOT>/td_bundle_<T>_*; never merged into one ls -dt; CWD is
                           never consulted when --output-dir is given. Result is TERMINAL --
                           an incomplete bundle exits 2 naming the module, never falls through.
                           The resolving branch is announced. Not-found never asserts absence.
New plugin version:        1.6.0
Skills changed:            report-renderer (only)
```

Under `Implemented:` record: the resume-by-default behavior and `--fresh-skeleton`; that the delta note is conditional on a resolvable prior; the acceptance results; and — **stated plainly** — that the Unit 2 reframing was wrong and the original §6 instinct was right, corrected on measurement.

- [ ] **Step 8: Report to the user**

Give the user: the release version, the suite count, the acceptance results including anything that failed, and the two backlog items (the unreferenced `run_pipeline.py` sharp edge; `render_report` writing a module JSON).

---

## Self-Review

**Spec coverage:** Unit 1 → Tasks 4 and 6. Unit 1b (D-R) → Task 5. Unit 2 → Tasks 1, 2, 3. Unit 3 → Tasks 1, 7. Unit 4 → Task 8. Unit 5 → Task 9. Guardrails G1/G2/G3 → Task 4 Step 2, asserted in Task 7. D7 → Task 6 Steps 3-4. The `--prev-dir` → `--previous` translation → Task 4 Step 1, exercised in Task 6.

**Naming consistency:** `word_budget.budget()` / `format_budget_block()` / `slot_budget()` / `open_slots()` / `section_contributors()` / `count_words()` / `page_sections()` / `countable_prose()` are used identically in Tasks 1, 2 and 3. `<BUNDLE>`, `<REPORT>`, `<PREV_BUNDLE>`, `<WORKROOT>`, `<TICKER_WS>` are used identically across Tasks 4-6.

**Deliberately not built (YAGNI):** a `--budget` mode on `render_report.py` for printing the block against an existing report on the resume path. `word_cap`'s failure message already covers that case well, and the resume path renders nothing.
