"""Tests for scripts/refresh_plan.py — the deterministic refresh planner.

WHY: A refresh re-runs an existing ticker workspace CHEAPLY via selective
FETCHING (never selective scoring). The planner is the deterministic brain of
that: given a previous bundle (manifest + snapshot) and an as-of date, it decides
per manifest group whether to REFETCH or REUSE, detects earnings/dividend events
that fell BETWEEN the two runs (which force a statement-set refetch and a judgment
re-affirmation), and estimates the refetch cost. Every rule is encoded as data and
unit-tested at its boundary so a plan can never silently reuse a stale group or
miss an event that should re-open the judgments.

The staleness windows are REUSED from scripts.qc._STALENESS_WINDOWS — the planner
and the QC gate must agree on what "in window" means, so the boundary tests here
double as a contract test against that table.

stdlib-only; unittest; each test fabricates an isolated tempdir ticker workspace.
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import date, timedelta

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLAN = os.path.join(REPO, "scripts", "refresh_plan.py")

if REPO not in sys.path:
    sys.path.insert(0, REPO)

from scripts import refresh_plan  # noqa: E402  (path set above)
from scripts import qc  # noqa: E402  (window contract)

AS_OF = "2026-07-16"


# --------------------------------------------------------------------------- #
# Fixture builders
# --------------------------------------------------------------------------- #

def _utc(day):
    """A retrieved_utc timestamp for a YYYY-MM-DD date string (noon UTC)."""
    return day + "T12:00:00Z"

def _days_before(anchor, n):
    """The YYYY-MM-DD string ``n`` days before ``anchor`` (YYYY-MM-DD)."""
    return (date.fromisoformat(anchor) - timedelta(days=n)).isoformat()


# The full set of manifest groups a normal alpha_vantage run records. Each maps
# to a retrieved_utc; the fixture ages them relative to the previous run's date.
_DEFAULT_GROUPS = (
    "global_quote", "overview", "daily_adjusted", "spy_daily_adjusted",
    "income_statement", "balance_sheet", "cash_flow", "earnings",
    "earnings_estimates", "news_sentiment", "insider_transactions",
    "options_chain", "pc_ratio_realtime", "earnings_calendar",
    "treasury_yield", "web_spot_check", "short_interest",
)


def _manifest(prev_as_of, ages=None, groups=_DEFAULT_GROUPS, data_mode="alpha_vantage"):
    """Build a previous-bundle manifest.

    ``ages`` maps a group -> age-in-days at prev_as_of (default 0 == same day as
    the previous run). The retrieved_utc is prev_as_of minus that age. Only groups
    in ``groups`` are recorded.
    """
    ages = ages or {}
    files = {}
    for g in groups:
        retrieved_day = _days_before(prev_as_of, ages.get(g, 0))
        files[g] = {
            "path": "raw/%s.json" % g,
            "endpoint_or_url": "AV:%s" % g,
            "retrieved_utc": _utc(retrieved_day),
        }
    manifest = {
        "ticker": "MU",
        "as_of_utc": _utc(prev_as_of),
        "data_mode": data_mode,
        "api_tier_notes": [],
        "files": files,
    }
    return manifest


def _snapshot(prev_as_of, next_earnings=None, ex_date=None,
              iv_newest_age=None):
    """Build a previous-bundle snapshot stub carrying the event fields the
    planner reads: events.next_earnings.date, events.dividends.ex_date, and an
    optional iv_history newest-sample age (via a sibling cache file)."""
    events = {
        "next_earnings": ({"date": next_earnings} if next_earnings else None),
        "dividends": {"ex_date": ex_date, "pay_date": None, "per_share": None},
        "catalysts": [],
    }
    return {
        "meta": {"ticker": "MU", "as_of_utc": _utc(prev_as_of)},
        "events": events,
    }


def _make_workspace(tmp, prev_as_of, ages=None, next_earnings=None, ex_date=None,
                    groups=_DEFAULT_GROUPS, legacy=False, iv_newest_age=None,
                    data_mode="alpha_vantage"):
    """Fabricate a ticker workspace with one previous bundle.

    Returns (ticker_dir, bundle_dir). ``legacy`` puts the bundle at the ticker
    dir root as a ``td_bundle_MU_<date>`` folder; otherwise the new
    ``trading_desk_MU/detail_reports_<date>/`` layout is used.
    """
    if legacy:
        ticker_dir = os.path.join(tmp, "td_bundle_MU_%s" % prev_as_of)
        bundle_dir = ticker_dir
    else:
        ticker_dir = os.path.join(tmp, "trading_desk_MU")
        bundle_dir = os.path.join(ticker_dir, "detail_reports_%s" % prev_as_of)
    os.makedirs(os.path.join(bundle_dir, "raw"), exist_ok=True)

    manifest = _manifest(prev_as_of, ages=ages, groups=groups, data_mode=data_mode)
    with open(os.path.join(bundle_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    snap = _snapshot(prev_as_of, next_earnings=next_earnings, ex_date=ex_date)
    with open(os.path.join(bundle_dir, "snapshot_MU_%s.json" % prev_as_of), "w") as fh:
        json.dump(snap, fh)

    # iv history cache in the ticker parent (new layout) — newest sample age.
    if iv_newest_age is not None and not legacy:
        newest = _days_before(prev_as_of, iv_newest_age)
        cache = {"ticker": "MU", "samples": [
            {"date": _days_before(prev_as_of, iv_newest_age + 30), "atm_iv": 0.4},
            {"date": newest, "atm_iv": 0.45},
        ]}
        with open(os.path.join(ticker_dir, "iv_history_MU.json"), "w") as fh:
            json.dump(cache, fh)
    return ticker_dir, bundle_dir


def _plan(ticker_dir, as_of=AS_OF):
    """Run the planner in-process and return the plan dict."""
    out = os.path.join(ticker_dir, "refresh_plan.json")
    rc = refresh_plan.main(["--ticker-dir", ticker_dir, "--as-of", as_of,
                            "--out", out])
    with open(out) as fh:
        return rc, json.load(fh)


# --------------------------------------------------------------------------- #
# Always-refetch set
# --------------------------------------------------------------------------- #

class AlwaysRefetchTests(unittest.TestCase):
    def test_always_refetch_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            # every group fresh (age 0) — the ALWAYS set still refetches.
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            groups = plan["groups"]
            for g in ("global_quote", "daily_adjusted", "spy_daily_adjusted",
                      "news_sentiment", "pc_ratio_realtime", "web_spot_check"):
                self.assertEqual(groups[g]["action"], "refetch", g)
                self.assertEqual(groups[g]["reason"], "always-refetch group", g)

    def test_options_chain_present_is_always_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["options_chain"]["action"], "refetch")
            self.assertEqual(plan["groups"]["options_chain"]["reason"],
                             "always-refetch group")

    def test_options_chain_absent_last_run_marked_refetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            groups = tuple(g for g in _DEFAULT_GROUPS if g != "options_chain")
            td, _ = _make_workspace(tmp, AS_OF, groups=groups)
            _, plan = _plan(td)
            oc = plan["groups"]["options_chain"]
            self.assertEqual(oc["action"], "refetch")
            self.assertEqual(oc["reason"], "absent last run")
            self.assertIsNone(oc["age_days"])


# --------------------------------------------------------------------------- #
# Window-based reuse vs refetch at boundary ages
# --------------------------------------------------------------------------- #

class WindowBoundaryTests(unittest.TestCase):
    def test_insider_reuse_at_89d(self):
        # insider window is 90d; 89d old at as_of -> reuse.
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF,
                                    ages={"insider_transactions": 89})
            _, plan = _plan(td)
            it = plan["groups"]["insider_transactions"]
            self.assertEqual(it["action"], "reuse")
            self.assertEqual(it["age_days"], 89)

    def test_insider_refetch_at_91d(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF,
                                    ages={"insider_transactions": 91})
            _, plan = _plan(td)
            it = plan["groups"]["insider_transactions"]
            self.assertEqual(it["action"], "refetch")
            self.assertEqual(it["age_days"], 91)

    def test_insider_reuse_exactly_at_window(self):
        # age == window (90d) -> reuse (<=, matching qc's own <= window rule).
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF,
                                    ages={"insider_transactions": 90})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["insider_transactions"]["action"],
                             "reuse")

    def test_short_interest_boundary(self):
        # short_interest window 14d.
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"short_interest": 14})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["short_interest"]["action"], "reuse")
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"short_interest": 15})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["short_interest"]["action"], "refetch")

    def test_treasury_yield_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"treasury_yield": 7})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["treasury_yield"]["action"], "reuse")
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"treasury_yield": 8})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["treasury_yield"]["action"], "refetch")

    def test_statement_group_reuse_when_fresh(self):
        # income_statement window 120d; a 30d-old copy is reused absent an event.
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"income_statement": 30})
            _, plan = _plan(td)
            inc = plan["groups"]["income_statement"]
            self.assertEqual(inc["action"], "reuse")
            self.assertIn("age 30d vs window 120d", inc["reason"])

    def test_reuse_reason_names_age_and_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"overview": 3})
            _, plan = _plan(td)
            ov = plan["groups"]["overview"]
            self.assertEqual(ov["action"], "reuse")
            self.assertIn("age 3d vs window 7d", ov["reason"])

    def test_window_table_matches_qc(self):
        # Contract: the planner must use qc's own windows, not a private copy.
        self.assertIs(refresh_plan._STALENESS_WINDOWS, qc._STALENESS_WINDOWS)


# --------------------------------------------------------------------------- #
# Earnings-between-runs event override
# --------------------------------------------------------------------------- #

class EarningsEventTests(unittest.TestCase):
    STATEMENT_SET = ("income_statement", "balance_sheet", "cash_flow",
                     "earnings", "earnings_estimates", "overview",
                     "earnings_calendar", "insider_transactions")

    def test_earnings_between_runs_forces_statement_set(self):
        # prev run 2026-06-01, as_of 2026-07-16, earnings 2026-06-20 (between).
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev,
                                    ages={g: 0 for g in self.STATEMENT_SET},
                                    next_earnings="2026-06-20")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertTrue(plan["events"]["earnings_between_runs"])
            self.assertEqual(plan["events"]["earnings_date"], "2026-06-20")
            self.assertTrue(plan["events"]["judgment_review_required"])
            for g in self.STATEMENT_SET:
                grp = plan["groups"][g]
                self.assertEqual(grp["action"], "refetch", g)
                self.assertEqual(grp["reason"], "event forces refetch", g)

    def test_earnings_before_previous_run_not_flagged(self):
        # earnings on 2026-05-15, BEFORE the previous run 2026-06-01 -> not an
        # event between runs; statement groups follow their windows (reuse).
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev,
                                    ages={g: 5 for g in self.STATEMENT_SET},
                                    next_earnings="2026-05-15")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertFalse(plan["events"]["earnings_between_runs"])
            self.assertFalse(plan["events"]["judgment_review_required"])
            self.assertEqual(plan["groups"]["income_statement"]["action"], "reuse")

    def test_earnings_after_as_of_not_flagged(self):
        # future earnings (after as_of) is not "between runs".
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev,
                                    ages={g: 5 for g in self.STATEMENT_SET},
                                    next_earnings="2026-08-20")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertFalse(plan["events"]["earnings_between_runs"])
            self.assertFalse(plan["events"]["judgment_review_required"])
            self.assertEqual(plan["groups"]["cash_flow"]["action"], "reuse")

    def test_earnings_on_previous_as_of_boundary_not_flagged(self):
        # earnings exactly ON the previous run date is NOT strictly-after prev,
        # so not "between (prev, as_of]".
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev,
                                    ages={g: 5 for g in self.STATEMENT_SET},
                                    next_earnings="2026-06-01")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertFalse(plan["events"]["earnings_between_runs"])

    def test_earnings_on_as_of_boundary_is_flagged(self):
        # earnings ON as_of is within (prev, as_of] -> flagged.
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev,
                                    ages={g: 5 for g in self.STATEMENT_SET},
                                    next_earnings="2026-07-16")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertTrue(plan["events"]["earnings_between_runs"])


# --------------------------------------------------------------------------- #
# Dividend override
# --------------------------------------------------------------------------- #

class DividendEventTests(unittest.TestCase):
    def test_dividend_ex_between_runs_forces_overview_and_calendar(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(
                tmp, prev,
                ages={"overview": 3, "earnings_calendar": 3, "cash_flow": 3},
                ex_date="2026-06-15")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertTrue(plan["events"]["dividend_ex_date_between_runs"])
            self.assertTrue(plan["events"]["judgment_review_required"])
            self.assertEqual(plan["groups"]["overview"]["action"], "refetch")
            self.assertEqual(plan["groups"]["overview"]["reason"],
                             "event forces refetch")
            self.assertEqual(plan["groups"]["earnings_calendar"]["action"],
                             "refetch")
            # dividend override does NOT touch cash_flow.
            self.assertEqual(plan["groups"]["cash_flow"]["action"], "reuse")

    def test_dividend_ex_future_not_flagged(self):
        with tempfile.TemporaryDirectory() as tmp:
            prev = "2026-06-01"
            td, _ = _make_workspace(tmp, prev, ages={"overview": 3},
                                    ex_date="2026-09-15")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertFalse(plan["events"]["dividend_ex_date_between_runs"])
            self.assertFalse(plan["events"]["judgment_review_required"])


# --------------------------------------------------------------------------- #
# iv_history refresh vs reuse
# --------------------------------------------------------------------------- #

class IvHistoryTests(unittest.TestCase):
    def test_iv_history_reuse_at_10d(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, iv_newest_age=10)
            _, plan = _plan(td)
            self.assertEqual(plan["iv_history"]["action"], "reuse")

    def test_iv_history_refresh_at_20d(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, iv_newest_age=20)
            _, plan = _plan(td)
            self.assertEqual(plan["iv_history"]["action"], "refresh")

    def test_iv_history_boundary_at_14d_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, iv_newest_age=14)
            _, plan = _plan(td)
            self.assertEqual(plan["iv_history"]["action"], "reuse")

    def test_iv_history_absent_reports_refresh(self):
        # No cache file at all -> refresh (there is nothing to reuse).
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF)  # no iv_newest_age
            _, plan = _plan(td)
            self.assertEqual(plan["iv_history"]["action"], "refresh")


# --------------------------------------------------------------------------- #
# Legacy layout + no-bundle
# --------------------------------------------------------------------------- #

class LayoutTests(unittest.TestCase):
    def test_legacy_td_bundle_located(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, bundle = _make_workspace(tmp, "2026-07-10", legacy=True)
            out = os.path.join(td, "refresh_plan.json")
            rc = refresh_plan.main(["--ticker-dir", td, "--as-of", AS_OF,
                                    "--out", out])
            self.assertEqual(rc, 0)
            with open(out) as fh:
                plan = json.load(fh)
            self.assertEqual(plan["previous_as_of"], "2026-07-10")
            self.assertTrue(plan["previous_bundle"].endswith(
                "td_bundle_MU_2026-07-10"))

    def test_newest_bundle_chosen_by_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, "2026-06-01")
            # add an older + a newer bundle sibling.
            _make_workspace(tmp, "2026-05-01")
            _make_workspace(tmp, "2026-07-01")
            _, plan = _plan(td)
            self.assertEqual(plan["previous_as_of"], "2026-07-01")
            self.assertTrue(plan["previous_bundle"].endswith(
                "detail_reports_2026-07-01"))

    def test_no_bundle_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = os.path.join(tmp, "trading_desk_MU")
            os.makedirs(empty)
            rc = refresh_plan.main(["--ticker-dir", empty, "--as-of", AS_OF])
            self.assertEqual(rc, 2)

    def test_missing_ticker_dir_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = refresh_plan.main(
                ["--ticker-dir", os.path.join(tmp, "nope"), "--as-of", AS_OF])
            self.assertEqual(rc, 2)


# --------------------------------------------------------------------------- #
# estimated_refetch_calls arithmetic + new_bundle naming + ticker
# --------------------------------------------------------------------------- #

class ArithmeticTests(unittest.TestCase):
    def test_estimated_refetch_calls_counts_refetch_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            # No events; everything not-always-refetch is fresh (reuse). The
            # always-refetch set present in the manifest: global_quote,
            # daily_adjusted, spy_daily_adjusted, news_sentiment,
            # pc_ratio_realtime, web_spot_check, options_chain = 7.
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            refetch = [g for g, v in plan["groups"].items()
                       if v["action"] == "refetch"]
            self.assertEqual(plan["estimated_refetch_calls"], len(refetch))
            self.assertEqual(len(refetch), 7)

    def test_estimated_calls_grows_with_stale_groups(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Age insider + short_interest past their windows: +2 refetches.
            td, _ = _make_workspace(
                tmp, AS_OF,
                ages={"insider_transactions": 200, "short_interest": 40})
            _, plan = _plan(td)
            self.assertEqual(plan["estimated_refetch_calls"], 9)

    def test_new_bundle_name_uses_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, "2026-07-01")
            _, plan = _plan(td, as_of="2026-07-16")
            self.assertEqual(plan["new_bundle"], "detail_reports_2026-07-16")
            self.assertEqual(plan["as_of"], "2026-07-16")
            self.assertEqual(plan["ticker"], "MU")


# --------------------------------------------------------------------------- #
# Determinism + plan file / stdout
# --------------------------------------------------------------------------- #

class DeterminismTests(unittest.TestCase):
    def test_determinism_fixed_as_of(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, "2026-06-01",
                                    ages={"insider_transactions": 50})
            _, plan_a = _plan(td, as_of="2026-07-16")
            _, plan_b = _plan(td, as_of="2026-07-16")
            self.assertEqual(plan_a, plan_b)

    def test_default_as_of_is_today(self):
        # With no --as-of the planner uses date.today(); the run must succeed and
        # the as_of must be a well-formed ISO date.
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, _days_before(date.today().isoformat(), 5))
            out = os.path.join(td, "refresh_plan.json")
            rc = refresh_plan.main(["--ticker-dir", td, "--out", out])
            self.assertEqual(rc, 0)
            with open(out) as fh:
                plan = json.load(fh)
            self.assertEqual(plan["as_of"], date.today().isoformat())

    def test_plan_written_to_default_path_and_printed(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, "2026-07-01")
            proc = subprocess.run(
                [sys.executable, PLAN, "--ticker-dir", td, "--as-of", AS_OF],
                capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            expected = os.path.join(td, "refresh_plan.json")
            self.assertTrue(os.path.isfile(expected))
            self.assertIn(expected, proc.stdout.strip())

    def test_out_flag_overrides_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, "2026-07-01")
            custom = os.path.join(tmp, "custom_plan.json")
            rc = refresh_plan.main(["--ticker-dir", td, "--as-of", AS_OF,
                                    "--out", custom])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.isfile(custom))


# --------------------------------------------------------------------------- #
# Previous-as-of + provenance fields
# --------------------------------------------------------------------------- #

class ProvenanceTests(unittest.TestCase):
    def test_previous_as_of_read_from_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, bundle = _make_workspace(tmp, "2026-07-01")
            _, plan = _plan(td)
            self.assertEqual(plan["previous_as_of"], "2026-07-01")
            self.assertEqual(os.path.normpath(plan["previous_bundle"]),
                             os.path.normpath(bundle))


if __name__ == "__main__":
    unittest.main()
