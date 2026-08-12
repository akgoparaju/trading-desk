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

### Technical — 69.5/100 (rubric v1.3.0) · ◐ MEDIUM

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
    # The flat fixture alone never exercises the nested-Data-Integrity branch (a
    # delta report nests "### Data Integrity" inside "## Data Integrity"); include
    # it so parity is checked on that path too, not just the common one.
    extra = "## Data Integrity\n### Data Integrity\n" + "x " * 20 + "\n### Tail\n y\n"
    for body in wb.page_sections(REPORT) + [extra]:
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


def test_section_contributors_are_ranked_largest_first():
    fat = REPORT.replace("Watch the print.", "word " * 200)
    rows = wb.section_contributors(fat, top_n=5)
    assert rows[0][0] == "Monitoring Protocol"
    assert [c for _, c in rows] == sorted((c for _, c in rows), reverse=True)


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


# --------------------------------------------------------------------------- #
# Non-report documents -- a delta report has no ``## Page`` headers, so
# check_word_cap's own ``_page_sections`` finds none and SKIPs the cap entirely.
# The dict and block must say so honestly instead of reporting a fabricated
# 0-word skeleton against a room that never actually applies.
# --------------------------------------------------------------------------- #

DELTA = """# PLTR Delta Report

### Summary

Position unchanged since last snapshot.

<!-- SLOT:delta_interpretation -->
"""


def test_budget_reports_not_capped_when_there_are_no_page_sections():
    assert wb.budget(DELTA)["capped"] is False


def test_format_budget_block_says_the_cap_does_not_apply_for_a_delta():
    block = wb.format_budget_block(wb.budget(DELTA))
    assert "does not apply" in block
