import unittest
from scripts import ev_kelly as E

SC = [{"name": "bull", "prob": 0.25, "price_target": 150.0},
      {"name": "base", "prob": 0.50, "price_target": 120.0},
      {"name": "bear", "prob": 0.25, "price_target": 80.0}]

class TestEV(unittest.TestCase):
    def test_ev_at_exact(self):
        # at entry 100: 0.25*0.5 + 0.5*0.2 + 0.25*(-0.2) = 0.175
        self.assertAlmostEqual(E.ev_at(SC, 100.0), 0.175)

    def test_probs_must_sum_to_one(self):
        bad = [dict(SC[0], prob=0.5), dict(SC[1], prob=0.6)]
        with self.assertRaises(ValueError): E.scenario_ev(bad)
        with self.assertRaises(ValueError): E.kelly(bad, 100.0)

    def test_kelly_exact(self):
        k = E.kelly(SC, 100.0)
        # p=0.75; win = (0.25*0.5+0.5*0.2)/0.75 = 0.3; loss = 0.2; b = 1.5
        self.assertAlmostEqual(k["p_win"], 0.75)
        self.assertAlmostEqual(k["b_odds"], 1.5)
        self.assertAlmostEqual(k["f_star"], 0.75 - 0.25 / 1.5)
        self.assertAlmostEqual(k["half"], k["f_star"] / 2)

    def test_kelly_edges(self):
        allwin = [{"name": "a", "prob": 1.0, "price_target": 120.0}]
        self.assertAlmostEqual(E.kelly(allwin, 100.0)["f_star"], 1.0)
        alllose = [{"name": "a", "prob": 1.0, "price_target": 80.0}]
        self.assertAlmostEqual(E.kelly(alllose, 100.0)["f_star"], 0.0)

    def test_size_caps_and_event_notch(self):
        s = E.size_recommendation(0.60, "balanced", False)
        self.assertAlmostEqual(s["recommended_pct"], 0.08)      # half-Kelly 0.30 > cap 0.08
        s2 = E.size_recommendation(0.60, "balanced", True)
        self.assertAlmostEqual(s2["recommended_pct"], 0.04)     # cap/2 binds
        s3 = E.size_recommendation(0.10, "long-term", False)
        self.assertAlmostEqual(s3["recommended_pct"], 0.05)     # half-Kelly binds
        with self.assertRaises(ValueError): E.size_recommendation(0.1, "yolo", False)

if __name__ == "__main__": unittest.main()
