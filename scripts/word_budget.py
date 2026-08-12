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
# Overshoot the trim by this much rather than converging onto the cap exactly. The
# measured tail on a real run was 2103 -> 2101 -> 2100, three QC cycles to recover
# three words. This changes only the TRIM INSTRUCTION, never the cap that is enforced.
WORD_TRIM_MARGIN = 40

# Per-slot authoring budgets. Two numbers mirror skills/report-renderer/SKILL.md:
# BRIEF_SLOT_BUDGET <-> "<=150 words each", and the 985 total <-> "Target <=1,000
# words of authored prose across all slots" -- change those together. The rest
# quantify SKILL.md's qualitative sizes ("1 sentence", "1-2 lines").
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

# Public: report_qc's own strict slot regex will alias onto this one so the two
# checks (the render-time budget and the blocking gate's check_no_empty_slots)
# cannot silently diverge on what counts as "open".
SLOT_RE = re.compile(r"<!--\s*SLOT:([a-z_]+)\s*-->")
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
    """``(total, counts)`` -- countable words, and one count per Page section
    found (possibly none, e.g. a delta report with no ``## Page`` headers)."""
    counts = [len(countable_prose(s).split()) for s in page_sections(report_text)]
    return sum(counts), counts


def open_slots(report_text):
    """Names of the slots still carrying an unfilled mark, in document order."""
    return SLOT_RE.findall(report_text)


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
        # No ``## Page`` sections (e.g. a delta report) => check_word_cap's own
        # _page_sections finds none and SKIPs -- the cap never actually runs, so
        # a 0-word skeleton and a "room" number here would be fabricated.
        "capped": bool(per_page),
        "open_slots": slots,
        "budgeted": budgeted,
        "oversubscribed": budgeted > room,
        "skeleton_near_cap": room < 0,
        "brief_slots_open": [s for s in slots if s.startswith("brief_")],
        "contributors": section_contributors(report_text, top_n=3),
    }


def format_budget_block(info):
    """The printable block. ASCII only -- this goes to a terminal."""
    cap, margin = info["cap"], info["margin"]
    skeleton = info["skeleton_words"]
    if not info["capped"]:
        lines = ["WORD BUDGET  no Page sections -- the word cap does not apply "
                 "(report_qc SKIPs word_cap on this document)."]
    else:
        lines = [
            f"WORD BUDGET  skeleton {skeleton} / cap {cap}   "
            f"(prose+headings; table rows and Data Integrity excluded)",
            f"  room for authored prose: {info['room']}   "
            f"(cap {cap} - margin {margin} - skeleton {skeleton})",
        ]

    slots = info["open_slots"]
    if slots:
        # Point the author at the slot budgets (985 total), not at the room
        # (1663 on the bundle this module exists because of): the failure this
        # module prevents was spending TO THE ROOM, not to the per-slot table.
        lines.append(f"  {len(slots)} open slot(s), budgeted {info['budgeted']} "
                     f"-- WRITE TO THIS, not to the room "
                     f"({info['room'] - info['budgeted']} words of slack).")
        for name in slots:
            lines.append(f"    {name:<24} {slot_budget(name)}")
    else:
        lines.append("  0 open slots -- every slot is filled or transcluded.")

    contrib = ", ".join(f"{n} {c}" for n, c in info["contributors"])
    warned = False
    if info["oversubscribed"]:
        lines.append(
            f"  !! OVER-SUBSCRIBED -- slot budgets ({info['budgeted']}) exceed the "
            f"room ({info['room']}). Writing to budget still fails the cap, so the "
            f"fix is upstream, not a tighter slot.")
        warned = True
    if info["skeleton_near_cap"]:
        lines.append(
            f"  !! SKELETON NEAR CAP -- {skeleton} of {cap} before a word of "
            f"prose.")
        warned = True
    if warned and contrib:
        lines.append(f"  Largest sections: {contrib}")
    if info["brief_slots_open"]:
        n = len(info["brief_slots_open"])
        lines.append(
            f"  NOTE  {n} brief slot(s) OPEN -- the brief's transclusion span "
            f"was not found, so ~{n * BRIEF_SLOT_BUDGET} words that normally "
            f"transclude must be authored here.")
    return "\n".join(lines)
