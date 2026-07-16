"""Governance test: each snapshot fact scores in exactly one evidence module.

WHY: the design spec's single-mapping rule says "each snapshot fact scores in
exactly one module". If two scorers ever list the same dotted snapshot path in
their ``INPUT_FIELDS``, the same fact would be double-counted across the composite
report -- silently inflating or deflating a ticker's overall read. This test pins
that rule mechanically: the scored-input sets must be pairwise disjoint, and no
scorer may score a field it only uses as a GATE/CAP condition (its GUARD_FIELDS).

stdlib-only; unittest. Imports the three scorer modules and compares their
declared field sets -- it does no scoring of its own.
"""

import unittest

from scripts import score_technical, score_risk, score_sentiment, score_fundamental

SKILLS = {"technical": score_technical, "risk": score_risk,
          "sentiment": score_sentiment, "fundamental": score_fundamental}


class TestSingleMapping(unittest.TestCase):
    def test_scored_input_sets_pairwise_disjoint(self):
        names = list(SKILLS)
        for i, a in enumerate(names):
            for b in names[i + 1:]:
                overlap = SKILLS[a].INPUT_FIELDS & SKILLS[b].INPUT_FIELDS
                self.assertFalse(overlap, f"{a} and {b} both score {overlap}")

    def test_guard_fields_never_scored_in_own_skill(self):
        for name, mod in SKILLS.items():
            guards = getattr(mod, "GUARD_FIELDS", set())
            self.assertFalse(guards & mod.INPUT_FIELDS,
                             f"{name} scores its own guard fields")


if __name__ == "__main__":
    unittest.main()
