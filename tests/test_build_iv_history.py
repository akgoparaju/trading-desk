"""Tests for scripts/build_iv_history.py (spec B18).

Correctness, not identity: there is no deterministic legacy IV value to match
(the old one-liner took spot from an unspecified LLM argument), so we assert the
emitted atm_iv equals a HAND-COMPUTED value at the raw '4. close' spot, and that
the raw close -- not the adjusted close -- is the ATM-selection spot.
"""

import json
import os
import tempfile
import unittest

from scripts import build_iv_history as B


def mk(exp, k, t, iv, mark=5.0, delta=0.5, oi=100):
    """One raw AV-shaped contract (string-typed fields, like chain fixtures)."""
    return {
        "expiration": exp, "strike": str(k), "type": t, "mark": str(mark),
        "bid": str(mark - 0.1), "ask": str(mark + 0.1),
        "implied_volatility": str(iv), "delta": str(delta),
        "open_interest": str(oi), "volume": "10",
    }


# A chain whose ATM strike DIFFERS between the raw close (100) and the adjusted
# close (50). At the ~30-DTE expiry, K=100 has call IV 0.50 / put IV 0.60
# (mean 0.55); K=50 has call IV 0.20 / put IV 0.30 (mean 0.25). So the emitted
# value discriminates which spot was used.
CHAIN_SPLIT_SENSITIVE = [
    mk("2026-02-13", 50, "call", iv=0.20),
    mk("2026-02-13", 50, "put", iv=0.30),
    mk("2026-02-13", 100, "call", iv=0.50),
    mk("2026-02-13", 100, "put", iv=0.60),
    # a far, wrong-DTE expiry that must NOT be picked over the ~30-DTE one
    mk("2026-06-19", 100, "call", iv=0.90),
    mk("2026-06-19", 100, "put", iv=0.90),
]


def daily_json(rows):
    """AV daily JSON: rows = {date: (close, adjusted_close)}."""
    ts = {
        d: {"1. open": str(c), "2. high": str(c), "3. low": str(c),
            "4. close": str(c), "5. adjusted close": str(a), "6. volume": "1000"}
        for d, (c, a) in rows.items()
    }
    return {"Time Series (Daily)": ts}


class TestBuildIVHistory(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        for name in os.listdir(self.tmp):
            try:
                os.unlink(os.path.join(self.tmp, name))
            except OSError:
                pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def _write(self, name, obj):
        path = os.path.join(self.tmp, name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return path

    def _chain(self, name, contracts):
        return self._write(name, {"data": contracts})

    # ---- correctness: atm_iv at the RAW '4. close' spot, not adjusted ------- #

    def test_atm_iv_uses_raw_close_not_adjusted(self):
        # sample date 2026-01-15: raw close 100 (ATM K=100 -> IV mean 0.55),
        # adjusted close 50 (ATM K=50 -> IV mean 0.25). Expect 0.55 -> raw wins.
        chain_file = self._chain("chain1.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": chain_file},
        ])
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-15": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")

        summary = B.build_iv_history(samples, daily, out, ticker="XX")
        self.assertEqual(summary["computed"], 1)

        cache = json.load(open(out))
        self.assertEqual(cache["ticker"], "XX")
        self.assertEqual(len(cache["samples"]), 1)
        self.assertEqual(cache["samples"][0]["date"], "2026-01-15")
        # (0.50 + 0.60) / 2 at K=100 (the raw-close ATM), NOT (0.20+0.30)/2 = 0.25
        self.assertAlmostEqual(cache["samples"][0]["atm_iv"], 0.55)

    def test_nearest_30dte_expiry_chosen_over_far_one(self):
        # sample 2026-01-15 -> ~30d target ~2026-02-14; 2026-02-13 wins over
        # 2026-06-19, so IV is 0.55 not 0.90.
        chain_file = self._chain("chain1.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": chain_file}])
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-15": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")
        B.build_iv_history(samples, daily, out)
        cache = json.load(open(out))
        self.assertAlmostEqual(cache["samples"][0]["atm_iv"], 0.55)

    # ---- empty / holiday chain is skipped with a recorded reason ----------- #

    def test_empty_chain_skipped_with_reason(self):
        # An empty chain file (holiday) -> chain.load_contracts raises -> skip.
        empty = self._write("empty.json", {"data": []})
        good = self._chain("good.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-10", "chain_file": empty},
            {"date": "2026-01-15", "chain_file": good},
        ])
        daily = self._write("daily_adjusted.json", daily_json({
            "2026-01-10": (100.0, 50.0), "2026-01-15": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")

        summary = B.build_iv_history(samples, daily, out)
        self.assertEqual(summary["computed"], 1)
        self.assertEqual(len(summary["skipped"]), 1)
        skip = summary["skipped"][0]
        self.assertEqual(skip["date"], "2026-01-10")
        self.assertTrue(skip["reason"])  # a non-empty recorded reason
        cache = json.load(open(out))
        self.assertEqual([s["date"] for s in cache["samples"]], ["2026-01-15"])

    def test_missing_daily_close_skipped_with_reason(self):
        good = self._chain("good.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": good}])
        # daily has a DIFFERENT date -> no close for the sample date.
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-14": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")
        summary = B.build_iv_history(samples, daily, out)
        self.assertEqual(summary["computed"], 0)
        self.assertEqual(len(summary["skipped"]), 1)
        self.assertIn("nominal close", summary["skipped"][0]["reason"])

    # ---- cache-merge dedupes by date --------------------------------------- #

    def test_cache_merge_dedupes_by_date(self):
        # Pre-existing cache with two dates; one overlaps a new sample.
        out = os.path.join(self.tmp, "iv_history_XX.json")
        with open(out, "w") as fh:
            json.dump({"ticker": "XX", "samples": [
                {"date": "2025-12-01", "atm_iv": 0.11},
                {"date": "2026-01-15", "atm_iv": 0.99},  # will be overwritten
            ]}, fh)

        chain_file = self._chain("chain1.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": chain_file}])
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-15": (100.0, 50.0)}))

        B.build_iv_history(samples, daily, out)
        cache = json.load(open(out))
        dates = [s["date"] for s in cache["samples"]]
        # deduped by date and sorted ascending
        self.assertEqual(dates, ["2025-12-01", "2026-01-15"])
        # the NEW computed value (0.55) overrides the old 0.99 for the dupe date
        overlap = next(s for s in cache["samples"] if s["date"] == "2026-01-15")
        self.assertAlmostEqual(overlap["atm_iv"], 0.55)
        # ticker preserved from the existing cache
        self.assertEqual(cache["ticker"], "XX")

    # ---- consumed chain files deleted on success --------------------------- #

    def test_consumed_chain_files_deleted_on_success(self):
        chain_file = self._chain("chain1.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": chain_file}])
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-15": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")

        self.assertTrue(os.path.exists(chain_file))
        summary = B.build_iv_history(samples, daily, out)
        self.assertIn(chain_file, summary["deleted"])
        self.assertFalse(os.path.exists(chain_file))  # deleted on success

    def test_skipped_chain_file_not_deleted(self):
        # A chain that gets skipped (no daily close) must NOT be deleted -- the
        # LLM may retry it; only consumed chains are removed.
        good = self._chain("good.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": good}])
        daily = self._write("daily_adjusted.json",
                            daily_json({"2026-01-14": (100.0, 50.0)}))
        out = os.path.join(self.tmp, "iv_history_XX.json")
        summary = B.build_iv_history(samples, daily, out)
        self.assertEqual(summary["deleted"], [])
        self.assertTrue(os.path.exists(good))  # skipped -> retained

    # ---- daily file lacking '4. close' is a clean error -------------------- #

    def test_daily_without_raw_close_is_error(self):
        good = self._chain("good.json", CHAIN_SPLIT_SENSITIVE)
        samples = self._write("iv_samples.json", [
            {"date": "2026-01-15", "chain_file": good}])
        # Daily bars with ONLY the adjusted close, no '4. close'.
        ts = {"2026-01-15": {"5. adjusted close": "50.0"}}
        daily = self._write("daily_adjusted.json", {"Time Series (Daily)": ts})
        out = os.path.join(self.tmp, "iv_history_XX.json")
        with self.assertRaises(B.IVHistoryError):
            B.build_iv_history(samples, daily, out)


if __name__ == "__main__":
    unittest.main()
