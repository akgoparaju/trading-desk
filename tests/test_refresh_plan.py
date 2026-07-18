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

import contextlib
import io
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


def _run_main(argv):
    """Run refresh_plan.main in-process, swallowing its CLI stdout/stderr.

    main() prints the output path to stdout on success and error strings to
    stderr; captured here so the unittest console stays clean (the plan is read
    back from the written file, so the printed path is not needed by tests)."""
    with contextlib.redirect_stdout(io.StringIO()), \
            contextlib.redirect_stderr(io.StringIO()):
        return refresh_plan.main(argv)


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
    rc = _run_main(["--ticker-dir", ticker_dir, "--as-of", as_of,
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

    def test_insider_refetch_exactly_at_window(self):
        # age == window (90d) -> REFETCH (strict <; review finding: the gate ages
        # with fractional days and strict '>', so an at-window reuse can fail when
        # the file's time-of-day precedes the new as_of's time-of-day).
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF,
                                    ages={"insider_transactions": 90})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["insider_transactions"]["action"],
                             "refetch")

    def test_planner_reuse_always_passes_gate_worst_case(self):
        # CROSS-CHECK: any planner-authorized reuse must pass qc.check_staleness
        # even in the worst time-of-day case (file retrieved at 00:00, new as_of
        # at 23:59 — maximal fractional age for a given integer date age).
        from scripts import qc as qc_mod
        for group, window in sorted(qc_mod._STALENESS_WINDOWS.items()):
            age = window - 1  # deepest age the strict planner rule can authorize
            with tempfile.TemporaryDirectory() as tmp:
                td, _ = _make_workspace(tmp, AS_OF, ages={group: age})
                _, plan = _plan(td)
                if plan["groups"].get(group, {}).get("action") != "reuse":
                    continue  # always-refetch groups etc.
                retrieved = plan["groups"][group]  # planner said reuse at window-1
                import datetime as _dt
                as_of_dt = _dt.date.fromisoformat(AS_OF)
                snapshot = {"meta": {
                    "as_of_utc": AS_OF + "T23:59:00Z",
                    "sources": [{"field_group": group,
                                 "endpoint_or_url": "x",
                                 "retrieved_utc": (as_of_dt - _dt.timedelta(days=age)).isoformat() + "T00:00:00Z",
                                 "covers": []}],
                }}
                res = qc_mod.check_staleness(snapshot)
                self.assertIsNot(res["passed"], False,
                                 f"{group}: planner reuse at {age}d failed the gate: {res['detail']}")

    def test_short_interest_boundary(self):
        # short_interest window 14d.
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"short_interest": 13})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["short_interest"]["action"], "reuse")
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"short_interest": 15})
            _, plan = _plan(td)
            self.assertEqual(plan["groups"]["short_interest"]["action"], "refetch")

    def test_treasury_yield_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF, ages={"treasury_yield": 6})
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
            rc = _run_main(["--ticker-dir", td, "--as-of", AS_OF,
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
            rc = _run_main(["--ticker-dir", empty, "--as-of", AS_OF])
            self.assertEqual(rc, 2)

    def test_missing_ticker_dir_exits_2(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc = _run_main(
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
            rc = _run_main(["--ticker-dir", td, "--out", out])
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
            rc = _run_main(["--ticker-dir", td, "--as-of", AS_OF,
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


# --------------------------------------------------------------------------- #
# Sector-scale falsifier monitoring (v0.12.0)
# --------------------------------------------------------------------------- #

import types  # noqa: E402


import scripts as _scripts_pkg  # noqa: E402  (to patch the submodule attribute)
_SENTINEL = object()


def _install_fake_sector_scales(test, results):
    """Inject a fake ``scripts.sector_scales`` whose evaluate_falsifiers returns
    ``results`` (a fixed list). Registered for cleanup so it never leaks between
    tests. ``results`` may be a callable(scale, snapshot) for per-scale control.

    NOTE: once ``scripts.sector_scales`` has been imported ANYWHERE (the real
    module now exists on disk), the ``scripts`` package holds a bound attribute and
    ``from scripts import sector_scales`` returns THAT, bypassing a sys.modules
    patch alone. So we patch BOTH sys.modules and the package attribute, and
    restore both.
    """
    fake = types.ModuleType("scripts.sector_scales")

    def evaluate_falsifiers(scale, snapshot):
        return results(scale, snapshot) if callable(results) else list(results)

    fake.evaluate_falsifiers = evaluate_falsifiers
    old_mod = sys.modules.get("scripts.sector_scales", _SENTINEL)
    old_attr = getattr(_scripts_pkg, "sector_scales", _SENTINEL)
    sys.modules["scripts.sector_scales"] = fake
    _scripts_pkg.sector_scales = fake

    def _restore():
        if old_mod is _SENTINEL:
            sys.modules.pop("scripts.sector_scales", None)
        else:
            sys.modules["scripts.sector_scales"] = old_mod
        if old_attr is _SENTINEL:
            if hasattr(_scripts_pkg, "sector_scales"):
                delattr(_scripts_pkg, "sector_scales")
        else:
            _scripts_pkg.sector_scales = old_attr
    test.addCleanup(_restore)


def _write_scale(scales_dir, name, version="2026-07-01", on_trip=None,
                 falsifiers=None):
    # ``scale`` is the identifier field in the sector_scales contract (not
    # ``name``); the fixture mirrors that so _scale_label reads the real key.
    os.makedirs(scales_dir, exist_ok=True)
    scale = {"scale": name, "version": version, "falsifiers": falsifiers or []}
    if on_trip is not None:
        scale["on_trip"] = on_trip
    with open(os.path.join(scales_dir, "%s.json" % name), "w") as fh:
        json.dump(scale, fh)


# A falsifier result set exercising all three tripped states.
_MIXED_FALSIFIERS = [
    {"metric": "iv_rank", "op": ">", "value": 80, "observed": 92,
     "tripped": True, "meaning": "positioning crowded"},
    {"metric": "breadth", "op": "<", "value": 0.3, "observed": 0.55,
     "tripped": False, "meaning": "breadth intact"},
    {"metric": "absent_metric", "op": ">", "value": 1, "observed": None,
     "tripped": None, "meaning": "metric absent from snapshot"},
]

_UNTRIPPED_ONLY = [
    {"metric": "breadth", "op": "<", "value": 0.3, "observed": 0.55,
     "tripped": False, "meaning": "breadth intact"},
    {"metric": "spread", "op": ">", "value": 5, "observed": 2,
     "tripped": False, "meaning": "tight"},
]


class ScaleFalsifierTests(unittest.TestCase):
    def _legacy_scales_dir(self, tmp):
        # ticker-dir-parent legacy location (deterministic without chdir).
        return os.path.join(tmp, "trading_desk_config", "scales")

    def test_tripped_scale_flips_review_and_names_consequence(self):
        _install_fake_sector_scales(self, _MIXED_FALSIFIERS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis", on_trip="re-base")
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertTrue(plan["scale_review_required"])
            scale = plan["scales"][0]
            self.assertEqual(scale["scale"], "semis@2026-07-01")
            self.assertTrue(scale["any_tripped"])
            self.assertEqual(len(scale["falsifiers"]), 3)
            self.assertIn("re-affirm or re-base", scale["action_required"])
            self.assertIn("re-base", scale["action_required"])

    def test_untripped_scale_action_none(self):
        _install_fake_sector_scales(self, _UNTRIPPED_ONLY)
        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis")
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertFalse(plan["scale_review_required"])
            scale = plan["scales"][0]
            self.assertFalse(scale["any_tripped"])
            self.assertEqual(scale["action_required"], "none")

    def test_default_on_trip_consequence(self):
        # scale JSON omits on_trip -> default flag+disclose in the action string.
        _install_fake_sector_scales(self, _MIXED_FALSIFIERS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis")  # no on_trip
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertIn("flag+disclose", plan["scales"][0]["action_required"])

    def test_unresolvable_falsifier_does_not_trip(self):
        # A scale whose ONLY falsifier is unresolvable (tripped None) is not a trip.
        _install_fake_sector_scales(
            self, [{"metric": "x", "op": ">", "value": 1, "observed": None,
                    "tripped": None, "meaning": "absent"}])
        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis")
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertFalse(plan["scales"][0]["any_tripped"])
            self.assertFalse(plan["scale_review_required"])
            self.assertEqual(plan["scales"][0]["action_required"], "none")

    def test_multiple_scales_any_tripped_flips_top_level(self):
        # One tripped, one clean -> scale_review_required True; per-scale honest.
        def by_name(scale, snapshot):
            if scale.get("scale") == "semis":
                return _MIXED_FALSIFIERS  # tripped
            return _UNTRIPPED_ONLY
        _install_fake_sector_scales(self, by_name)
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._legacy_scales_dir(tmp)
            _write_scale(sd, "semis", on_trip="re-base")
            _write_scale(sd, "software")
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertTrue(plan["scale_review_required"])
            by = {s["scale"].split("@")[0]: s for s in plan["scales"]}
            self.assertTrue(by["semis"]["any_tripped"])
            self.assertFalse(by["software"]["any_tripped"])

    def test_absent_scales_dir_empty_block(self):
        # No trading_desk_config/scales at all -> empty scales, review False.
        _install_fake_sector_scales(self, _MIXED_FALSIFIERS)  # module present
        with tempfile.TemporaryDirectory() as tmp:
            td, _ = _make_workspace(tmp, AS_OF)  # no scales dir written
            _, plan = _plan(td)
            self.assertEqual(plan["scales"], [])
            self.assertFalse(plan["scale_review_required"])
            self.assertEqual(plan["pending_proposals"], [])

    def test_lazy_import_degradation_module_absent(self):
        # Hide scripts.sector_scales entirely -> falsifiers not evaluated, no crash;
        # the scale still appears with an empty falsifier list + a skip note.
        # Force an ImportError by mapping the module name to None in sys.modules AND
        # removing the package attribute (which `from scripts import ...` prefers
        # once the real module has been imported anywhere).
        old_mod = sys.modules.get("scripts.sector_scales", _SENTINEL)
        old_attr = getattr(_scripts_pkg, "sector_scales", _SENTINEL)
        sys.modules["scripts.sector_scales"] = None  # import -> ImportError
        if hasattr(_scripts_pkg, "sector_scales"):
            delattr(_scripts_pkg, "sector_scales")

        def _restore():
            if old_mod is _SENTINEL:
                sys.modules.pop("scripts.sector_scales", None)
            else:
                sys.modules["scripts.sector_scales"] = old_mod
            if old_attr is not _SENTINEL:
                _scripts_pkg.sector_scales = old_attr
        self.addCleanup(_restore)

        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis", on_trip="re-base")
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertEqual(len(plan["scales"]), 1)
            scale = plan["scales"][0]
            self.assertEqual(scale["falsifiers"], [])
            self.assertFalse(scale["any_tripped"])
            self.assertEqual(scale["action_required"], "none")
            self.assertIn("unavailable", scale.get("note", ""))
            self.assertFalse(plan["scale_review_required"])

    def test_pending_proposals_listed(self):
        _install_fake_sector_scales(self, _UNTRIPPED_ONLY)
        with tempfile.TemporaryDirectory() as tmp:
            sd = self._legacy_scales_dir(tmp)
            _write_scale(sd, "semis")
            proposals = os.path.join(sd, "proposals")
            os.makedirs(proposals)
            for fn in ("semis_2026-08-01.json", "software_2026-08-02.json"):
                with open(os.path.join(proposals, fn), "w") as fh:
                    json.dump({"status": "pending_ratification"}, fh)
            td, _ = _make_workspace(tmp, AS_OF)
            _, plan = _plan(td)
            self.assertEqual(
                plan["pending_proposals"],
                ["semis_2026-08-01.json", "software_2026-08-02.json"])

    def test_scale_review_parallel_to_judgment_review(self):
        # A tripped scale must NOT alter judgment_review_required (unchanged logic):
        # no earnings/dividend between runs -> judgment_review stays False even as
        # scale_review flips True.
        _install_fake_sector_scales(self, _MIXED_FALSIFIERS)
        with tempfile.TemporaryDirectory() as tmp:
            _write_scale(self._legacy_scales_dir(tmp), "semis", on_trip="re-base")
            td, _ = _make_workspace(tmp, AS_OF)  # no events
            _, plan = _plan(td)
            self.assertTrue(plan["scale_review_required"])
            self.assertFalse(plan["events"]["judgment_review_required"])


class ScaleCwdTests(unittest.TestCase):
    """Exercise the CWD-primary scales location by actually running from cwd."""

    def test_cwd_scales_found_via_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            # Lay a scales dir + a stub sector_scales on the ticker-parent legacy
            # path so a real subprocess (fresh interpreter, no injected module)
            # degrades gracefully -- the scale still appears with a skip note.
            _write_scale(os.path.join(tmp, "trading_desk_config", "scales"),
                         "semis", on_trip="re-base")
            td, _ = _make_workspace(tmp, AS_OF)
            proc = subprocess.run(
                [sys.executable, PLAN, "--ticker-dir", td, "--as-of", AS_OF],
                capture_output=True, text=True, cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            with open(os.path.join(td, "refresh_plan.json")) as fh:
                plan = json.load(fh)
            # The scale is discovered (cwd == tmp, so cwd/trading_desk_config/scales
            # exists). Whether sector_scales exists in the repo determines the
            # falsifier list; either way the scale entry is present + review is a
            # bool, and the block never crashes.
            self.assertEqual(len(plan["scales"]), 1)
            self.assertEqual(plan["scales"][0]["scale"], "semis@2026-07-01")
            self.assertIn("scale_review_required", plan)
            self.assertIsInstance(plan["scale_review_required"], bool)


if __name__ == "__main__":
    unittest.main()
