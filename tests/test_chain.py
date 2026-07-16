import json, os, tempfile, unittest
from scripts import chain as C

def mk(exp, k, t, mark=5.0, iv=0.5, delta=0.5, oi=100):
    return {"expiration": exp, "strike": str(k), "type": t, "mark": str(mark),
            "bid": str(mark - 0.1), "ask": str(mark + 0.1),
            "implied_volatility": str(iv), "delta": str(delta), "open_interest": str(oi), "volume": "10"}

CHAIN = [
    mk("2026-08-21", 90,  "put",  mark=2.0, iv=0.60, delta=-0.25, oi=500),
    mk("2026-08-21", 100, "put",  mark=5.0, iv=0.55, delta=-0.45, oi=1000),
    mk("2026-08-21", 100, "call", mark=6.0, iv=0.50, delta=0.55,  oi=800),
    mk("2026-08-21", 110, "call", mark=2.5, iv=0.48, delta=0.25,  oi=2000),
    mk("2026-09-18", 100, "call", mark=8.0, iv=0.45, delta=0.50,  oi=300),
    mk("2026-09-18", 100, "put",  mark=7.0, iv=0.47, delta=-0.50, oi=400),
]

class TestChain(unittest.TestCase):
    def _write(self, obj, suffix=".json"):
        f = tempfile.NamedTemporaryFile("w", suffix=suffix, delete=False); json.dump(obj, f); f.close()
        self.addCleanup(os.unlink, f.name); return f.name

    def test_load_raw_list_and_data_key_and_mcp_envelope(self):
        for payload in (CHAIN, {"data": CHAIN}, {"content": [{"type": "text", "text": json.dumps({"data": CHAIN})}]}):
            cs = C.load_contracts(self._write(payload))
            self.assertEqual(len(cs), 6)
            self.assertIsInstance(cs[0]["strike"], float)   # normalized types
            self.assertEqual(cs[0]["type"], "put")

    def test_load_rejects_garbage(self):
        with self.assertRaises(ValueError):
            C.load_contracts(self._write({"note": "rate limit"}))

    def test_atm_iv(self):
        cs = C.load_contracts(self._write(CHAIN))
        self.assertAlmostEqual(C.atm_iv(cs, 101.0, "2026-08-21"), 0.525)  # (0.50+0.55)/2 at K=100

    def test_expected_move(self):
        cs = C.load_contracts(self._write(CHAIN))
        em = C.expected_move(cs, 100.0, "2026-08-21")
        self.assertAlmostEqual(em["straddle"], 11.0)          # 6.0 + 5.0
        self.assertAlmostEqual(em["one_sigma"], 9.35)         # 0.85 x 11
        self.assertAlmostEqual(em["range_high"], 109.35)

    def test_max_pain(self):
        cs = C.load_contracts(self._write(CHAIN))
        # payouts at candidates: S=90 -> puts (100-90)*1000 = 10000; S=100 -> 0; S=110 -> calls (110-100)*800 = 8000
        self.assertAlmostEqual(C.max_pain(cs, "2026-08-21"), 100.0)

    def test_oi_walls(self):
        cs = C.load_contracts(self._write(CHAIN))
        w = C.oi_walls(cs, "2026-08-21", 100.0)
        self.assertEqual(w["call_wall"]["strike"], 110.0)     # max call OI strictly above spot
        self.assertEqual(w["put_wall"]["strike"], 100.0)      # max put OI at-or-below spot: K=100 (1000) beats K=90 (500)
        self.assertLessEqual(len(w["near_money_clusters"]), 3)

    def test_put_call_ratio(self):
        cs = C.load_contracts(self._write(CHAIN))
        self.assertAlmostEqual(C.put_call_ratio(cs, "2026-08-21"), 1500 / 2800)
        self.assertAlmostEqual(C.put_call_ratio(cs), 1900 / 3100)

    def test_skew(self):
        cs = C.load_contracts(self._write(CHAIN))
        self.assertAlmostEqual(C.skew_25d(cs, 100.0, "2026-08-21"), 0.60 - 0.48)  # 25-delta put IV - 25-delta call IV

if __name__ == "__main__":
    unittest.main()
