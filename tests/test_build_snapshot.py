"""Tests for scripts/build_snapshot.py and scripts/qc_gate.py.

WHY: build_snapshot.py is the ONLY path from raw Alpha Vantage response files to
the numeric fields the LLM later reasons over. If the arithmetic here is wrong,
every downstream trade decision inherits the error silently. So the fixture
fabricates a full bundle in the VERIFIED live-API shapes with hand-computed
expected sums, and the tests assert the builder reproduces them exactly. The
options chain, estimates, insider rows, and preview-wrapped treasury file each
exercise a distinct parsing branch.

stdlib-only; unittest; each test builds an isolated tempdir bundle.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BUILD = os.path.join(REPO, "scripts", "build_snapshot.py")
GATE = os.path.join(REPO, "scripts", "qc_gate.py")

AS_OF = "2026-07-16T20:10:00Z"
AS_OF_DATE = "2026-07-16"

# --- deterministic geometric-walk OHLCV -------------------------------------

def _walk(n, seed, start=100.0):
    """Deterministic geometric random walk of ``n`` daily bars (oldest-first).

    A tiny LCG keeps this stdlib-free and reproducible so expected indicator
    values are stable across runs/machines.
    """
    state = seed & 0xFFFFFFFF
    price = start
    rows = []
    # Trading days ending at 2026-07-15 (business-day-ish; weekends skipped).
    import datetime as _dt
    day = _dt.date(2026, 7, 15)
    dates = []
    while len(dates) < n:
        if day.weekday() < 5:
            dates.append(day.isoformat())
        day = day - _dt.timedelta(days=1)
    dates.reverse()  # oldest-first
    for i in range(n):
        state = (1103515245 * state + 12345) & 0x7FFFFFFF
        # unit-ish jitter in [-0.01, +0.011)
        r = ((state / 0x7FFFFFFF) - 0.5) * 0.021 + 0.0005
        price = price * (1 + r)
        close = round(price, 4)
        high = round(close * 1.01, 4)
        low = round(close * 0.99, 4)
        openp = round(close * 0.999, 4)
        vol = 1_000_000 + (state % 500_000)
        rows.append({
            "date": dates[i], "open": openp, "high": high, "low": low,
            "close": close, "adj": close, "volume": vol,
        })
    return rows


def _daily_json(rows):
    """Build a TIME_SERIES_DAILY_ADJUSTED payload (NEWEST-first keys)."""
    ts = {}
    for r in rows:
        ts[r["date"]] = {
            "1. open": f"{r['open']}",
            "2. high": f"{r['high']}",
            "3. low": f"{r['low']}",
            "4. close": f"{r['close']}",
            "5. adjusted close": f"{r['adj']}",
            "6. volume": f"{r['volume']}",
            "7. dividend amount": "0.0000",
            "8. split coefficient": "1.0",
        }
    return {"Meta Data": {"2. Symbol": "MU"}, "Time Series (Daily)": ts}


def _stooq_csv(rows):
    """Build a stooq daily-export CSV string (header ``Date,Open,High,Low,Close,
    Volume``, ASCENDING date order like stooq's own export). No adjusted column --
    stooq closes are already split-adjusted, which the builder discloses via the
    ``series_source`` label. Rows are already oldest-first from _walk."""
    lines = ["Date,Open,High,Low,Close,Volume"]
    for r in rows:
        vol = int(r["volume"])
        lines.append(f"{r['date']},{r['open']},{r['high']},{r['low']},"
                     f"{r['close']},{vol}")
    return "\r\n".join(lines) + "\r\n"


class BundleBuilder:
    """Fabricates a full raw-response bundle and manifest on disk."""

    def __init__(self, root, ticker="MU"):
        self.root = root
        self.ticker = ticker
        self.raw = os.path.join(root, "raw")
        os.makedirs(self.raw, exist_ok=True)
        self.files = {}
        self.stock_rows = _walk(320, seed=101, start=90.0)
        self.spy_rows = _walk(320, seed=202, start=400.0)
        self.last = self.stock_rows[-1]["close"]
        self.last_date = self.stock_rows[-1]["date"]
        self.shares = 1_100_000_000.0  # SharesOutstanding
        self.mktcap = self.last * self.shares

    # -- generic writers ----------------------------------------------------
    def _write(self, name, obj):
        path = os.path.join(self.raw, name)
        with open(path, "w") as fh:
            json.dump(obj, fh)
        return os.path.join("raw", name)

    def _write_text(self, name, text):
        """Write a bare (non-JSON) text file, e.g. a stooq CSV export."""
        path = os.path.join(self.raw, name)
        with open(path, "w") as fh:
            fh.write(text)
        return os.path.join("raw", name)

    def _add(self, key, name, obj, endpoint):
        rel = self._write(name, obj)
        self.files[key] = {
            "path": rel, "endpoint_or_url": endpoint, "retrieved_utc": AS_OF,
        }

    def _add_rel(self, key, rel, endpoint):
        """Register an already-written file (by relative path) in the manifest."""
        self.files[key] = {
            "path": rel, "endpoint_or_url": endpoint, "retrieved_utc": AS_OF,
        }

    # -- REQUIRED files -----------------------------------------------------
    def add_global_quote(self):
        gq = {"Global Quote": {
            "01. symbol": self.ticker,
            "05. price": f"{self.last}",
            "08. previous close": f"{self.stock_rows[-2]['close']}",
            "03. high": f"{self.stock_rows[-1]['high']}",
            "04. low": f"{self.stock_rows[-1]['low']}",
            "07. latest trading day": self.last_date,
        }}
        self._add("global_quote", "global_quote.json", gq, "GLOBAL_QUOTE")

    def add_overview(self):
        ov = {
            "Symbol": self.ticker,
            "MarketCapitalization": f"{self.mktcap:.0f}",
            "SharesOutstanding": f"{self.shares:.0f}",
            "EPS": "6.00",
            "PERatio": f"{self.last / 6.0:.4f}",
            "ForwardPE": "18.5",
            "PEGRatio": "1.20",
            "EVToEBITDA": "14.3",
            "ReturnOnEquityTTM": "0.28",
            "AnalystTargetPrice": f"{self.last * 1.15:.2f}",
            "AnalystRatingStrongBuy": "10",
            "AnalystRatingBuy": "8",
            "AnalystRatingHold": "5",
            "AnalystRatingSell": "1",
            "AnalystRatingStrongSell": "0",
            "52WeekHigh": "140.00",
            "52WeekLow": "60.00",
            "Beta": "1.30",
            "DividendPerShare": "0.46",
            "DividendDate": "2026-08-15",
            "ExDividendDate": "2026-07-25",
        }
        self._add("overview", "overview.json", ov, "COMPANY_OVERVIEW")

    def add_daily(self):
        self._add("daily_adjusted", "daily.json", _daily_json(self.stock_rows),
                  "TIME_SERIES_DAILY_ADJUSTED")

    def add_spy(self):
        self._add("spy_daily_adjusted", "spy_daily.json", _daily_json(self.spy_rows),
                  "TIME_SERIES_DAILY_ADJUSTED")

    # -- stooq CSV daily variants (no-Alpha-Vantage fallback) ---------------
    def add_daily_csv_bare(self):
        """Stock daily series as a BARE .csv stooq export file."""
        rel = self._write_text("daily.csv", _stooq_csv(self.stock_rows))
        self._add_rel("daily_adjusted", rel, "stooq CSV")

    def add_daily_csv_wrapped(self):
        """Stock daily series as {"result": "<csv>"} (CSV-in-JSON envelope)."""
        self._add("daily_adjusted", "daily_csv.json",
                  {"result": _stooq_csv(self.stock_rows)}, "stooq CSV")

    def add_spy_csv_bare(self):
        rel = self._write_text("spy_daily.csv", _stooq_csv(self.spy_rows))
        self._add_rel("spy_daily_adjusted", rel, "stooq CSV")

    def add_spy_csv_wrapped(self):
        self._add("spy_daily_adjusted", "spy_daily_csv.json",
                  {"result": _stooq_csv(self.spy_rows)}, "stooq CSV")

    # -- web-transcribed fundamentals (no-Alpha-Vantage fallback) -----------
    def add_web_fundamentals(self, payload):
        """Register a web_fundamentals transcription file with the given payload."""
        self._add("web_fundamentals", "web_fundamentals.json", payload,
                  "web (cited)")

    # -- fundamentals (5 quarterly reports, known sums) ---------------------
    def add_income(self):
        # newest-first. rev_ttm = sum of first 4. Same-qtr-prior-year = index 4.
        q = [
            {"fiscalDateEnding": "2026-06-30", "totalRevenue": "8000", "grossProfit": "4000",
             "operatingIncome": "2000", "netIncome": "1500"},
            {"fiscalDateEnding": "2026-03-31", "totalRevenue": "7000", "grossProfit": "3500",
             "operatingIncome": "1800", "netIncome": "1300"},
            {"fiscalDateEnding": "2025-12-31", "totalRevenue": "6000", "grossProfit": "3000",
             "operatingIncome": "1500", "netIncome": "1100"},
            {"fiscalDateEnding": "2025-09-30", "totalRevenue": "5000", "grossProfit": "2500",
             "operatingIncome": "1200", "netIncome": "900"},
            {"fiscalDateEnding": "2025-06-30", "totalRevenue": "4000", "grossProfit": "2000",
             "operatingIncome": "1000", "netIncome": "700"},
        ]
        self._add("income_statement", "income.json",
                  {"symbol": self.ticker, "annualReports": [], "quarterlyReports": q},
                  "INCOME_STATEMENT")

    def add_balance(self):
        q = [
            {"fiscalDateEnding": "2026-06-30",
             "cashAndShortTermInvestments": "9000",
             "cashAndCashEquivalentsAtCarryingValue": "6000",
             "shortTermInvestments": "3000",
             "longTermInvestments": "2000",
             "shortLongTermDebtTotal": "4000",
             "shortTermDebt": "1000", "longTermDebt": "3000"},
            {"fiscalDateEnding": "2026-03-31",
             "cashAndShortTermInvestments": "8500",
             "longTermInvestments": "1900",
             "shortLongTermDebtTotal": "4100"},
        ]
        self._add("balance_sheet", "balance.json",
                  {"symbol": self.ticker, "annualReports": [], "quarterlyReports": q},
                  "BALANCE_SHEET")

    def add_cashflow(self):
        # fcf_ttm = sum4q (ocf - capex). capex positive here.
        q = [
            {"fiscalDateEnding": "2026-06-30", "operatingCashflow": "3000", "capitalExpenditures": "1000"},
            {"fiscalDateEnding": "2026-03-31", "operatingCashflow": "2800", "capitalExpenditures": "900"},
            {"fiscalDateEnding": "2025-12-31", "operatingCashflow": "2600", "capitalExpenditures": "800"},
            {"fiscalDateEnding": "2025-09-30", "operatingCashflow": "2400", "capitalExpenditures": "700"},
            {"fiscalDateEnding": "2025-06-30", "operatingCashflow": "2000", "capitalExpenditures": "600"},
        ]
        self._add("cash_flow", "cashflow.json",
                  {"symbol": self.ticker, "annualReports": [], "quarterlyReports": q},
                  "CASH_FLOW")

    def add_earnings(self):
        # QC3: reportedEPS sums to 6.00, matching overview EPS "6.00" exactly
        # (0% check_eps divergence) -- was 5.20 (13.3% off), a pre-existing
        # fixture inconsistency that check_eps's new three-way reconciliation
        # now correctly surfaces. Only the first quarter changed (1.60 -> 2.40).
        q = [
            {"fiscalDateEnding": "2026-06-30", "reportedDate": "2026-07-01", "reportedEPS": "2.40"},
            {"fiscalDateEnding": "2026-03-31", "reportedDate": "2026-04-01", "reportedEPS": "1.40"},
            {"fiscalDateEnding": "2025-12-31", "reportedDate": "2026-01-05", "reportedEPS": "1.20"},
            {"fiscalDateEnding": "2025-09-30", "reportedDate": "2025-10-05", "reportedEPS": "1.00"},
        ]
        self._add("earnings", "earnings.json",
                  {"symbol": self.ticker, "annualEarnings": [], "quarterlyEarnings": q},
                  "EARNINGS")

    def add_estimates(self):
        # 2 FUTURE quarters (<4) + 1 FUTURE fiscal year => nearest_future_fiscal_year.
        est = [
            {"date": "2026-09-30", "horizon": "fiscal quarter",
             "eps_estimate_average": "1.70", "eps_estimate_high": "1.9", "eps_estimate_low": "1.5",
             "eps_estimate_analyst_count": "12", "eps_estimate_average_90_days_ago": "1.60",
             "eps_estimate_revision_up_trailing_30_days": "4",
             "eps_estimate_revision_down_trailing_30_days": "1",
             "revenue_estimate_average": "8500"},
            {"date": "2026-12-31", "horizon": "fiscal quarter",
             "eps_estimate_average": "1.80", "eps_estimate_average_90_days_ago": "1.70",
             "eps_estimate_revision_up_trailing_30_days": "3",
             "eps_estimate_revision_down_trailing_30_days": "2",
             "revenue_estimate_average": "9000"},
            {"date": "2027-06-30", "horizon": "fiscal year",
             "eps_estimate_average": "7.50", "eps_estimate_high": "8.5", "eps_estimate_low": "6.5",
             "eps_estimate_analyst_count": "20", "eps_estimate_average_90_days_ago": "7.00",
             "eps_estimate_revision_up_trailing_30_days": "9",
             "eps_estimate_revision_down_trailing_30_days": "3",
             "revenue_estimate_average": "34000"},
        ]
        self._add("earnings_estimates", "estimates.json",
                  {"symbol": self.ticker, "estimates": est}, "EARNINGS_ESTIMATES")

    def add_news(self):
        self._add("news_sentiment", "news.json",
                  {"items": "5", "feed": [{"title": "x", "overall_sentiment_score": "0.2"}]},
                  "NEWS_SENTIMENT")

    def add_insider(self):
        # priced A: +100*50=+5000; priced D: -40*60=-2400; empty-price A excluded;
        # old priced row (outside 90d) excluded. Net = +2600.
        data = [
            {"transaction_date": "2026-07-10", "executive": "CEO", "executive_title": "CEO",
             "security_type": "Common", "acquisition_or_disposal": "A",
             "shares": "100.0", "share_price": "50.0"},
            {"transaction_date": "2026-06-01", "executive": "CFO", "executive_title": "CFO",
             "security_type": "Common", "acquisition_or_disposal": "D",
             "shares": "40.0", "share_price": "60.0"},
            {"transaction_date": "2026-05-15", "executive": "VP", "executive_title": "VP",
             "security_type": "RSU", "acquisition_or_disposal": "A",
             "shares": "500.0", "share_price": ""},
            {"transaction_date": "2026-01-01", "executive": "OLD", "executive_title": "Dir",
             "security_type": "Common", "acquisition_or_disposal": "A",
             "shares": "999.0", "share_price": "10.0"},
        ]
        self._add("insider_transactions", "insider.json", {"data": data},
                  "INSIDER_TRANSACTIONS")

    def add_chain(self, with_date=True):
        d = self.last_date if with_date else None
        def c(exp, k, t, mark, iv, delta, oi):
            row = {"expiration": exp, "strike": str(k), "type": t, "mark": str(mark),
                   "implied_volatility": str(iv), "delta": str(delta),
                   "open_interest": str(oi), "volume": "5"}
            if d:
                row["date"] = d
            return row
        # Expiries roughly 30 / 60 / 90 days out from 2026-07-16.
        chain = [
            c("2026-08-14", 100, "put", 4.0, 0.55, -0.45, 1000),
            c("2026-08-14", 100, "call", 5.0, 0.50, 0.55, 900),
            c("2026-08-14", 110, "call", 2.0, 0.48, 0.25, 1500),
            c("2026-08-14", 90, "put", 1.5, 0.60, -0.25, 700),
            c("2026-09-18", 100, "put", 6.0, 0.52, -0.48, 400),
            c("2026-09-18", 100, "call", 7.0, 0.47, 0.52, 500),
            c("2026-10-16", 100, "put", 8.0, 0.50, -0.50, 300),
            c("2026-10-16", 100, "call", 9.0, 0.45, 0.50, 350),
        ]
        self._add("options_chain", "chain.json", {"data": chain}, "HISTORICAL_OPTIONS")
        # store fixture chain P/C for assertion (all-expiry): puts oi / calls oi
        puts = sum(r["open_interest"] and float(r["open_interest"]) for r in chain if r["type"] == "put")
        calls = sum(float(r["open_interest"]) for r in chain if r["type"] == "call")
        self.chain_pc = puts / calls
        # volume-based P/C (the realtime comparand): puts volume / calls volume
        put_vol = sum(float(r["volume"]) for r in chain if r["type"] == "put")
        call_vol = sum(float(r["volume"]) for r in chain if r["type"] == "call")
        self.chain_pc_volume = put_vol / call_vol

    def add_pc(self):
        # realtime P/C is volume-based: within 0.15 of the chain's VOLUME P/C
        rt = round(self.chain_pc_volume + 0.05, 4)
        self._add("pc_ratio_realtime", "pc.json", {
            "symbol": self.ticker,
            "put_call_ratio_full_chain": f"{rt}",
            "put_call_ratio_by_expiration": [
                {"date": "2026-08-14", "value": "0.70"},
                {"date": "2026-09-18", "value": "0.80"},
            ],
        }, "REALTIME_PUT_CALL_RATIO")

    def add_earnings_calendar(self):
        csv = ("symbol,name,reportDate,fiscalDateEnding,estimate,currency,timeOfTheDay\r\n"
               "MU,MICRON,2026-09-25,2026-08-31,1.88,USD,post-market\r\n")
        self._add("earnings_calendar", "ecal.json", {"result": csv}, "EARNINGS_CALENDAR")

    def add_treasury(self):
        # PREVIEW-WRAPPED: exercises unpreview.
        inner = {"name": "10year", "interval": "daily", "unit": "percent",
                 "data": [{"date": "2026-07-15", "value": "4.25"},
                          {"date": "2026-07-14", "value": "4.20"}]}
        wrapped = {"preview": True, "sample_data": json.dumps(inner), "data_truncated": True}
        self._add("treasury_yield", "treasury.json", wrapped, "TREASURY_YIELD")

    def add_web_spot(self):
        spot = round(self.last * 1.005, 2)  # within 1.5%
        self._add("web_spot_check", "web_spot.json",
                  {"price": spot, "source_url": "https://example.com/MU"}, "web")

    def add_short_interest(self):
        self._add("short_interest", "short_interest.json",
                  {"short_interest_pct": 2.4, "si_trend": "rising", "as_of": "2026-07-10",
                   "source_url": "https://example.com/si"}, "web")

    def add_iv_history(self):
        samples = [{"date": f"2026-{m:02d}-01", "atm_iv": round(0.40 + 0.01 * m, 4)}
                   for m in range(1, 13)]
        path = os.path.join(self.root, f"iv_history_{self.ticker}.json")
        with open(path, "w") as fh:
            json.dump({"ticker": self.ticker, "samples": samples}, fh)
        self.iv_history_rel = f"iv_history_{self.ticker}.json"

    def write_manifest(self, data_mode=None, data_source=None):
        m = {"ticker": self.ticker, "as_of_utc": AS_OF,
             "api_tier_notes": ["premium 75rpm"], "files": self.files}
        if getattr(self, "iv_history_rel", None):
            m["iv_history_path"] = self.iv_history_rel
        if data_mode is not None:
            m["data_mode"] = data_mode
        if data_source is not None:
            m["data_source"] = data_source
        with open(os.path.join(self.root, "manifest.json"), "w") as fh:
            json.dump(m, fh)

    def build_full(self):
        self.add_global_quote(); self.add_overview(); self.add_daily(); self.add_spy()
        self.add_income(); self.add_balance(); self.add_cashflow(); self.add_earnings()
        self.add_estimates(); self.add_news(); self.add_insider(); self.add_chain()
        self.add_pc(); self.add_earnings_calendar(); self.add_treasury()
        self.add_web_spot(); self.add_short_interest(); self.add_iv_history()
        self.write_manifest()
        return self


def _run_build(bundle, ticker="MU", extra=None):
    cmd = [sys.executable, BUILD, "--bundle", bundle, "--ticker", ticker]
    if extra:
        cmd += extra
    return subprocess.run(cmd, capture_output=True, text=True)


def _run_gate(snapshot_path, waivers=None):
    cmd = [sys.executable, GATE, snapshot_path]
    for w in (waivers or []):
        cmd += ["--waive", w]
    return subprocess.run(cmd, capture_output=True, text=True)


class TestBuildSnapshotFull(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp()
        cls.b = BundleBuilder(cls.dir).build_full()
        cls.proc = _run_build(cls.dir)
        # locate output
        cls.out = os.path.join(cls.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        if os.path.exists(cls.out):
            with open(cls.out) as fh:
                cls.snap = json.load(fh)
        else:
            cls.snap = None

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir, ignore_errors=True)

    def test_exit_zero_and_output_exists(self):
        self.assertEqual(self.proc.returncode, 0,
                         f"stderr={self.proc.stderr}\nstdout={self.proc.stdout}")
        self.assertTrue(os.path.exists(self.out))
        self.assertIn(self.out, self.proc.stdout)

    def test_meta_fields(self):
        m = self.snap["meta"]
        self.assertEqual(m["ticker"], "MU")
        self.assertEqual(m["as_of_utc"], AS_OF)
        self.assertEqual(m["schema_version"], "0.5.0")  # QC Phase 1: 0.4.0 -> 0.5.0
        self.assertEqual(m["missing"], [])
        self.assertIn("qc", m)
        self.assertTrue(len(m["sources"]) >= 4)

    def test_mktcap_computed_exact(self):
        self.assertAlmostEqual(self.snap["price"]["mktcap_computed"],
                               self.b.last * self.b.shares, places=2)

    def test_price_fields(self):
        p = self.snap["price"]
        self.assertAlmostEqual(p["last"], self.b.last, places=4)
        self.assertAlmostEqual(p["shares_diluted_m"], self.b.shares / 1e6, places=6)
        self.assertEqual(len(p["intraday_range"]), 2)
        # QC2: wk52_high/low are DERIVED from the bundle's own bars (364-day
        # window anchored on the last bar), NOT the vendor overview aggregate
        # (140.00/60.00 in this fixture) -- reproduce the window here rather
        # than hard-code a value that depends on the random-walk fixture.
        import datetime as _dt
        last_d = _dt.date.fromisoformat(self.b.last_date)
        window_start = last_d - _dt.timedelta(days=364)
        windowed = [r for r in self.b.stock_rows
                   if window_start <= _dt.date.fromisoformat(r["date"]) <= last_d]
        self.assertAlmostEqual(p["wk52_high"], max(r["high"] for r in windowed))
        self.assertAlmostEqual(p["wk52_low"], min(r["low"] for r in windowed))
        self.assertEqual(p["hi_lo_basis"]["method"], "derived_intraday_high_low")
        self.assertAlmostEqual(p["hi_lo_basis"]["vendor_high"], 140.0)
        self.assertAlmostEqual(p["hi_lo_basis"]["vendor_low"], 60.0)
        self.assertIsNotNone(p["web_spot_check"])

    def test_fundamentals_ttm_sums(self):
        f = self.snap["fundamentals"]
        self.assertAlmostEqual(f["rev_ttm"], 8000 + 7000 + 6000 + 5000)  # 26000
        self.assertAlmostEqual(f["gm_ttm"], (4000 + 3500 + 3000 + 2500) / 26000)
        self.assertAlmostEqual(f["om_ttm"], (2000 + 1800 + 1500 + 1200) / 26000)
        self.assertAlmostEqual(f["nm_ttm"], (1500 + 1300 + 1100 + 900) / 26000)
        # fcf_ttm = sum (ocf - capex)
        self.assertAlmostEqual(f["fcf_ttm"],
                               (3000 - 1000) + (2800 - 900) + (2600 - 800) + (2400 - 700))
        # rev_growth latest / same-qtr-prior-year: 8000/4000 - 1
        self.assertAlmostEqual(f["rev_growth_latest_q"], 8000 / 4000 - 1)
        # eps_ttm from overview; computed from 4 quarterly reportedEPS
        self.assertAlmostEqual(f["eps_ttm"], 6.0)
        self.assertAlmostEqual(f["eps_ttm_computed"], 2.40 + 1.40 + 1.20 + 1.00)

    def test_net_cash(self):
        nc = self.snap["fundamentals"]["net_cash_defined"]
        self.assertAlmostEqual(nc["cash_st"], 9000)
        self.assertAlmostEqual(nc["lt_inv"], 2000)
        self.assertAlmostEqual(nc["total_debt"], 4000)
        self.assertAlmostEqual(nc["net"], 9000 + 2000 - 4000)

    def test_eps_ntm_method_is_degraded_single_fy(self):
        # QC1: the fixture carries only ONE future fiscal-year estimate row
        # (2027-06-30), whose ~1yr span CONTAINS as_of (2026-07-16) -- so it is
        # the CURRENT fiscal year, not a second point to blend against. With
        # <4 future quarters AND <2 future fiscal years, the builder falls back
        # to the single-FY proxy, but the method string must now DISCLOSE the
        # degradation (pre-QC1 this was mislabeled "nearest_future_fiscal_year").
        f = self.snap["fundamentals"]
        self.assertEqual(f["eps_ntm_method"], "single_future_fiscal_year_degraded_proxy")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 7.50)
        self.assertIn("DEGRADED", f["eps_ntm_basis"])
        self.assertIsNone(f["eps_ntm_coverage"])  # coverage only meaningful for the blend

    def test_revisions_from_future_fy(self):
        rev = self.snap["fundamentals"]["revisions_90d"]
        self.assertAlmostEqual(rev["eps_now"], 7.50)
        self.assertAlmostEqual(rev["eps_90d_ago"], 7.00)
        self.assertAlmostEqual(rev["pct"], 7.50 / 7.00 - 1)
        self.assertAlmostEqual(rev["up_30d"], 9)
        self.assertAlmostEqual(rev["down_30d"], 3)

    def test_next_fy_consensus_is_none_when_only_current_fy_present(self):
        # QC1: next_fy_consensus is the FY estimate AFTER the one whose span
        # contains as_of. The fixture's only future-FY row (2027-06-30) IS that
        # current-FY row (its span 2026-07-01..2027-06-30 contains as_of
        # 2026-07-16), so there is genuinely no "next" FY estimate available --
        # next_fy_consensus must be None (disclosed absence), not the
        # mislabeled current-FY row the pre-QC1 code shipped (rev=34000,
        # eps=7.50).
        f = self.snap["fundamentals"]
        self.assertIsNone(f["next_fy_consensus"])
        basis = f["next_fy_basis"]
        self.assertEqual(basis["current_fy_end"], "2027-06-30")
        self.assertIsNone(basis["next_fy_end"])

    def test_technicals_ranges(self):
        t = self.snap["technicals"]
        self.assertTrue(0 < t["rsi14"] < 100)
        self.assertEqual(t["ohlcv_rows"], 320)
        self.assertEqual(t["last_ohlcv_date"], self.b.last_date)
        self.assertIsNotNone(t["ma50"])
        self.assertIsNotNone(t["ma200"])
        self.assertGreater(t["rv20_ann"], 0)
        self.assertIsInstance(t["drawdowns_by_year"], list)

    def test_ret_15d_matches_pct_return(self):
        # schema 0.2.1 adds technicals.ret_15d for the Phase-2 vertical-rally
        # penalty (15 sessions, distinct from ret_1m's 21). Assert it equals
        # indicators.pct_return over the fixture's adjusted closes at lookback 15.
        from scripts import indicators
        adj = [r["adj"] for r in self.b.stock_rows]  # adj == close in fixture
        expected = indicators.pct_return(adj, 15)
        self.assertIsNotNone(expected)
        self.assertAlmostEqual(self.snap["technicals"]["ret_15d"], expected,
                               places=9)

    def test_benchmark(self):
        bm = self.snap["benchmark"]
        self.assertIsNotNone(bm["spy_ret_1m"])
        # QC5: beta is now a 5y-MONTHLY estimate. The fixture's ~320 daily
        # bars (~15 calendar months) fall below the 24-monthly-observation
        # floor -- beta/corr are correctly WITHHELD (None) rather than
        # reported from an unreliably short window, disclosed in beta_basis.
        self.assertIsNone(bm["beta"])
        self.assertIsNone(bm["corr"])
        self.assertIsNone(bm["beta_n_obs"])
        self.assertIsNone(bm["beta_n_days"])
        self.assertEqual(bm["beta_basis"]["method"], "5y_monthly")
        self.assertIn("degraded_reason", bm["beta_basis"])
        # overview.Beta ("1.30" in the fixture) ingested as a counterparty.
        self.assertAlmostEqual(bm["beta_vendor"], 1.30)

    def test_valuation(self):
        v = self.snap["valuation"]
        self.assertAlmostEqual(v["pe_ttm"], self.b.last / 6.0)
        self.assertAlmostEqual(v["pe_fwd"], self.b.last / 7.50)
        # QC12: pe_5yr_median/pe_10yr_median are now an era-correct rolling-TTM
        # P/E (was "approx_current_eps", today's single eps_ttm back-projected
        # across the whole price history).
        self.assertEqual(v["pe_median_method"], "rolling_ttm_reported_eps")
        self.assertIsNotNone(v["fcf_yield"])
        # QC1: overview.ForwardPE is ingested as a cross-vendor counterparty.
        self.assertAlmostEqual(v["pe_overview_fwd"], 18.5)
        # QC12: this fixture's earnings history (4 quarters, earliest
        # reportedDate 2025-10-05) starts AFTER the 320-bar window's first bar
        # (~2025-04-24) -- no quarter is usable at the window start, so the
        # PRIMARY input-evaluability gate correctly withholds trust even
        # though a (thin, ~11-point) median is still computed and disclosed.
        self.assertFalse(v["pe_5yr_evaluable"])
        self.assertIn("insufficient_eps_series", v["pe_5yr_evaluability_reason"])
        self.assertIsNotNone(v["pe_5yr_median"])
        # D2/D3: price_basis now truthfully says "split-adjusted" (was the
        # bare "raw close" label paired with a now-corrected note that had
        # asserted a premise falsified by AAPL's real reportedEPS history).
        self.assertEqual(v["pe_median_basis"]["price_basis"],
                         "split-adjusted close (never dividend-adjusted)")
        self.assertEqual(v["pe_median_basis"]["method"], "rolling_ttm_reported_eps")

    def test_options_block(self):
        o = self.snap["options"]
        self.assertEqual(o["chain_as_of"], self.b.last_date)
        self.assertTrue(len(o["expected_moves"]) >= 1)
        self.assertTrue(len(o["max_pain_by_expiry"]) >= 1)
        self.assertIsNotNone(o["oi_walls"])
        self.assertIn("raw", o["chain_file_path"])

    def test_sentiment_pc_and_iv(self):
        s = self.snap["sentiment"]
        self.assertAlmostEqual(s["put_call_ratio_full_chain"], self.b.chain_pc, places=4)
        self.assertAlmostEqual(s["put_call_ratio_full_chain_volume"],
                               self.b.chain_pc_volume, places=4)
        self.assertIsNotNone(s["put_call_ratio_realtime"])
        self.assertTrue(len(s["put_call_by_expiry"]) >= 1)
        self.assertIsNotNone(s["iv30"])
        # ratings sum
        self.assertEqual(s["ratings"]["n"], 10 + 8 + 5 + 1 + 0)
        self.assertIsNotNone(s["consensus_pt"])
        self.assertAlmostEqual(s["short_interest_pct"], 2.4)
        self.assertEqual(s["si_trend"], "rising")

    def test_insider_net_90d(self):
        s = self.snap["sentiment"]
        # +100*50 - 40*60 = +2600 ; empty-price + old row excluded
        self.assertAlmostEqual(s["insider_net_90d_usd"], 2600.0)

    def test_events_next_earnings_from_csv(self):
        ev = self.snap["events"]
        self.assertEqual(ev["next_earnings"]["date"], "2026-09-25")
        self.assertEqual(ev["next_earnings"]["time"], "post-market")
        self.assertAlmostEqual(ev["next_earnings"]["consensus_eps"], 1.88)
        self.assertAlmostEqual(ev["dividends"]["per_share"], 0.46)
        self.assertEqual(ev["dividends"]["ex_date"], "2026-07-25")

    def test_macro_treasury_from_preview(self):
        mac = self.snap["macro"]
        self.assertIsNotNone(mac["treasury_10y"])
        self.assertAlmostEqual(mac["treasury_10y"]["value"], 4.25)
        self.assertEqual(mac["treasury_10y"]["date"], "2026-07-15")

    def test_llm_slots_null(self):
        self.assertIsNone(self.snap["sentiment"]["news_sentiment_summary"])
        self.assertIsNone(self.snap["sentiment"]["inst_flow_notes"])
        self.assertEqual(self.snap["events"]["catalysts"], [])

    def test_av_json_path_has_no_series_source_label(self):
        # The AV-JSON daily path is the default: no stooq disclosure label.
        self.assertNotIn("series_source", self.snap["technicals"])

    def test_data_mode_defaults_to_alpha_vantage(self):
        # No data_mode in the manifest -> meta.data_mode defaults to alpha_vantage.
        self.assertEqual(self.snap["meta"]["data_mode"], "alpha_vantage")

    def test_data_source_defaults_to_alphavantage(self):
        # No data_source in the manifest -> meta.data_source defaults to alphavantage.
        self.assertEqual(self.snap["meta"]["data_source"], "alphavantage")

    def test_no_web_transcribed_fields_when_statements_present(self):
        # Statement-derived fundamentals with no web_fundamentals file ->
        # the web_transcribed_fields disclosure array is empty.
        self.assertEqual(self.snap["fundamentals"]["web_transcribed_fields"], [])


# --------------------------------------------------------------------------- #
# QC2: 52-week high/low DERIVED from the bundle's own bars (364-day window,
# intraday high/low), vendor overview kept as a reconciliation counterparty.
# --------------------------------------------------------------------------- #

class TestQC2Wk52DerivedRange(unittest.TestCase):
    """build_price derives wk52_high/low from ``rows`` intraday high/low over a
    364-day window anchored on the LAST bar (not as_of, which may be a
    non-trading day), never straight from vendor overview.52WeekHigh/Low.
    AAPL 2026-08-07 ground truth: vendor low 223.15 matches NO window of the
    bundle's own bars; derived low is 216.58."""

    @staticmethod
    def _quote_and_overview(vendor_high, vendor_low):
        quote = {"Global Quote": {
            "05. price": "230.0", "08. previous close": "229.0",
            "03. high": "231.0", "04. low": "228.0",
        }}
        overview = {"52WeekHigh": f"{vendor_high}", "52WeekLow": f"{vendor_low}"}
        return quote, overview

    def test_derived_low_pinned_to_364day_boundary_aapl_shape(self):
        from scripts import build_snapshot as bs
        import datetime as _dt

        last_date = _dt.date(2026, 8, 6)  # mirrors the real AAPL last bar
        n_back = 400  # calendar days of daily filler, comfortably above the
                      # minimum-bars threshold so the derivation stays live.
        rows = []
        for offset in range(n_back, -1, -1):
            d = last_date - _dt.timedelta(days=offset)
            rows.append({
                "date": d.isoformat(), "open": 250.0, "high": 250.5,
                "low": 249.5, "close": 250.0, "adjusted_close": 250.0,
                "volume": 1_000_000,
            })

        day_364 = (last_date - _dt.timedelta(days=364)).isoformat()
        day_365 = (last_date - _dt.timedelta(days=365)).isoformat()
        for r in rows:
            if r["date"] == day_364:
                r["low"] = 216.58  # the window's true low: INCLUDED (boundary day)
            if r["date"] == day_365:
                r["low"] = 205.59  # one day OUTSIDE the window: must be EXCLUDED

        quote, overview = self._quote_and_overview(344.57, 223.15)
        price = bs.build_price(quote, overview, rows, None)

        # The defect: vendor 223.15 matches no window of the bundle's own bars.
        self.assertAlmostEqual(price["wk52_low"], 216.58)
        self.assertNotAlmostEqual(price["wk52_low"], 223.15)
        # The 365-day-prior bar (lower still, at 205.59) must be EXCLUDED --
        # if it leaked into the window the derived low would be 205.59.
        self.assertNotAlmostEqual(price["wk52_low"], 205.59)

        basis = price["hi_lo_basis"]
        self.assertEqual(basis["method"], "derived_intraday_high_low")
        self.assertEqual(basis["window_days"], 364)
        self.assertEqual(basis["anchor_date"], last_date.isoformat())
        self.assertEqual(basis["window_start"], day_364)
        self.assertEqual(basis["source"], "daily_adjusted intraday high/low")
        # Vendor kept as a reconciliation counterparty -- never overwritten.
        self.assertAlmostEqual(basis["vendor_high"], 344.57)
        self.assertAlmostEqual(basis["vendor_low"], 223.15)
        self.assertLess(basis["low_delta_pct_vs_vendor"], 0)  # vendor overstates the low

    def test_derived_high_ignores_vendor_when_bars_disagree(self):
        # MU 2026-08-07 shape: derived high 1255.00 vs vendor 1254.81.
        from scripts import build_snapshot as bs
        import datetime as _dt
        last_date = _dt.date(2026, 8, 7)
        rows = []
        for offset in range(400, -1, -1):
            d = last_date - _dt.timedelta(days=offset)
            rows.append({
                "date": d.isoformat(), "open": 1000.0, "high": 1000.5,
                "low": 999.5, "close": 1000.0, "adjusted_close": 1000.0,
                "volume": 500_000,
            })
        # Plant the true high inside the window, away from the vendor figure.
        rows[-40]["high"] = 1255.00
        quote, overview = self._quote_and_overview(1254.81, 113.28)
        price = bs.build_price(quote, overview, rows, None)
        self.assertAlmostEqual(price["wk52_high"], 1255.00)
        self.assertEqual(price["hi_lo_basis"]["vendor_high"], 1254.81)

    def test_fallback_to_vendor_when_too_few_bars(self):
        from scripts import build_snapshot as bs
        # Only a handful of bars -- nowhere near a real 52-week window --
        # must fall back to vendor, LOUDLY disclosed (never silent).
        rows = [
            {"date": "2026-08-04", "open": 10.0, "high": 10.5, "low": 9.5,
             "close": 10.0, "adjusted_close": 10.0, "volume": 1000},
            {"date": "2026-08-05", "open": 10.1, "high": 10.6, "low": 9.6,
             "close": 10.1, "adjusted_close": 10.1, "volume": 1000},
            {"date": "2026-08-06", "open": 10.2, "high": 10.7, "low": 9.7,
             "close": 10.2, "adjusted_close": 10.2, "volume": 1000},
        ]
        quote, overview = self._quote_and_overview(15.0, 8.0)
        price = bs.build_price(quote, overview, rows, None)
        self.assertAlmostEqual(price["wk52_high"], 15.0)
        self.assertAlmostEqual(price["wk52_low"], 8.0)
        basis = price["hi_lo_basis"]
        self.assertIn("insufficient", basis["method"])
        self.assertTrue(basis["method"].startswith("fallback"))
        self.assertEqual(basis["bars_in_window"], 3)

    def test_fallback_on_empty_rows(self):
        from scripts import build_snapshot as bs
        quote, overview = self._quote_and_overview(15.0, 8.0)
        price = bs.build_price(quote, overview, [], None)
        self.assertAlmostEqual(price["wk52_high"], 15.0)
        self.assertAlmostEqual(price["wk52_low"], 8.0)
        self.assertTrue(price["hi_lo_basis"]["method"].startswith("fallback"))


# --------------------------------------------------------------------------- #
# QC4: net cash BOTH legs -- built from COMPONENTS, vendor aggregates kept
# only as a reconciliation counterparty (both are measured-broken: LEG1
# cashAndShortTermInvestments silently drops shortTermInvestments; LEG2
# shortLongTermDebtTotal silently drops shortTermDebt on MU).
# --------------------------------------------------------------------------- #

class TestQC4NetCashComponents(unittest.TestCase):
    """_build_net_cash built from components, exact AAPL/MU ground truth
    (fiscalDateEnding 2026-06-30 / 2026-05-31 balance sheets)."""

    def test_aapl_component_values_and_vendor_defect_routed_around(self):
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "39544000000",
            "shortTermInvestments": "22855000000",
            "longTermInvestments": "84118000000",
            "shortTermDebt": "12967000000",
            "longTermDebt": "71340000000",
            # leases absent (None) on AAPL.
            "cashAndShortTermInvestments": "39544000000",   # BROKEN: == cce alone
            "shortLongTermDebtTotal": "84307000000",         # happens to be correct here
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["cash_and_equivalents"], 39_544_000_000.0)
        self.assertAlmostEqual(nc["short_term_investments"], 22_855_000_000.0)
        self.assertAlmostEqual(nc["cash_st"], 62_399_000_000.0)
        self.assertAlmostEqual(nc["lt_inv"], 84_118_000_000.0)
        self.assertAlmostEqual(nc["short_term_debt"], 12_967_000_000.0)
        self.assertAlmostEqual(nc["long_term_debt"], 71_340_000_000.0)
        self.assertAlmostEqual(nc["total_debt"], 84_307_000_000.0)
        self.assertIsNone(nc["capital_lease_obligations"])
        # CORRECTED net, matching the review's stated 62.2B exactly.
        self.assertAlmostEqual(nc["net"], 62_210_000_000.0)
        # The shipped (buggy) net must NEVER be produced again.
        self.assertNotAlmostEqual(nc["net"], 39_355_000_000.0)
        # No leases -> ex-lease and incl-lease conventions coincide.
        self.assertAlmostEqual(nc["net_incl_leases"], nc["net"])
        # Vendor aggregates preserved verbatim for reconciliation.
        self.assertAlmostEqual(
            nc["vendor_aggregates"]["cash_and_short_term_investments"], 39_544_000_000.0)
        self.assertAlmostEqual(
            nc["vendor_aggregates"]["short_long_term_debt_total"], 84_307_000_000.0)

    def test_mu_component_values_and_lease_disclosure(self):
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "24995000000",
            "shortTermInvestments": "1027000000",
            "longTermInvestments": "4106000000",
            "shortTermDebt": "582000000",
            "longTermDebt": "3052000000",
            "capitalLeaseObligations": "3324000000",
            "cashAndShortTermInvestments": "24995000000",   # BROKEN: == cce alone
            "shortLongTermDebtTotal": "6376000000",          # BROKEN: omits shortTermDebt
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["cash_st"], 26_022_000_000.0)
        self.assertAlmostEqual(nc["total_debt"], 3_634_000_000.0)
        self.assertAlmostEqual(nc["capital_lease_obligations"], 3_324_000_000.0)
        # CORRECTED net, ex-lease.
        self.assertAlmostEqual(nc["net"], 26_494_000_000.0)
        # net_incl_leases makes the lease convention visible too.
        self.assertAlmostEqual(nc["net_incl_leases"], 23_170_000_000.0)
        # The shipped (buggy) net must NEVER be produced again.
        self.assertNotAlmostEqual(nc["net"], 22_725_000_000.0)
        self.assertAlmostEqual(
            nc["vendor_aggregates"]["cash_and_short_term_investments"], 24_995_000_000.0)
        self.assertAlmostEqual(
            nc["vendor_aggregates"]["short_long_term_debt_total"], 6_376_000_000.0)

    def test_fallback_to_vendor_when_every_component_of_a_leg_absent(self):
        from scripts import build_snapshot as bs
        bal = {
            "cashAndShortTermInvestments": "8500",
            "longTermInvestments": "1900",
            "shortLongTermDebtTotal": "4100",
            # cce/sti/std/ltd all absent -> both legs fall back to vendor.
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["cash_st"], 8500.0)
        self.assertAlmostEqual(nc["total_debt"], 4100.0)
        self.assertIn("FALLBACK", nc["basis"])
        self.assertIn("cashAndShortTermInvestments", nc["basis"])
        self.assertIn("shortLongTermDebtTotal", nc["basis"])

    def test_xom_shape_incomplete_components_falls_back_to_vendor(self):
        # QC4-REGRESSION (found by the 22-ticker survey): the QC4 fix's rule
        # "fall back to vendor ONLY when EVERY component is absent" leaves a
        # gap when longTermDebt is None but shortTermDebt is populated -- the
        # component sum silently uses shortTermDebt ALONE (10.139B), massively
        # UNDERSTATING debt vs the real ~42.368B, which OVERSTATES net cash.
        # shortLongTermDebtTotal materially exceeds the visible partial sum
        # here, so this is "components INCOMPLETE", not "vendor uses a
        # different definition" -- total_debt must fall back to the vendor
        # aggregate (the conservative direction).
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "10000000000",
            "shortTermInvestments": "2000000000",
            "longTermInvestments": "1000000000",
            "shortTermDebt": "10139000000",
            "longTermDebt": None,
            "shortLongTermDebtTotal": "42368000000",
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["total_debt"], 42_368_000_000.0)
        # The shipped-regression (buggy) number must NEVER be produced again.
        self.assertNotAlmostEqual(nc["total_debt"], 10_139_000_000.0)
        # Both numbers stay visible: the partial component AND the vendor figure.
        self.assertAlmostEqual(nc["short_term_debt"], 10_139_000_000.0)
        self.assertIsNone(nc["long_term_debt"])
        self.assertAlmostEqual(
            nc["vendor_aggregates"]["short_long_term_debt_total"], 42_368_000_000.0)
        self.assertIn("INCOMPLETE", nc["basis"])

    def test_reverse_shape_std_absent_ltd_present_falls_back_to_vendor(self):
        # Symmetric case (GOOG's real shape): shortTermDebt absent, longTermDebt
        # present. The rule must not be std-specific -- EITHER component absent
        # triggers the same incomplete-components fallback when the vendor
        # aggregate materially exceeds the visible partial sum.
        #
        # D4: the adopted vendor aggregate here (112,756,000,000) is EXACTLY
        # longTermDebt (98,165,000,000) + capitalLeaseObligations
        # (14,591,000,000) -- the vendor's "matches-(b)" convention, which is
        # LEASE-INCLUSIVE, not the "financial debt, excludes capital/finance
        # leases" convention the generic basis text claims. total_debt's
        # NUMERIC VALUE is unchanged (still the best available proxy without
        # a real shortTermDebt), but basis must say so truthfully and
        # net_incl_leases must NOT subtract capital_lease_obligations a
        # SECOND time (they are already inside this total_debt).
        from scripts import build_snapshot as bs
        bal = {
            "shortTermDebt": None,
            "longTermDebt": "98165000000",
            "capitalLeaseObligations": "14591000000",
            "shortLongTermDebtTotal": "112756000000",
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["total_debt"], 112_756_000_000.0)
        self.assertIn("INCOMPLETE", nc["basis"])
        self.assertTrue(nc["total_debt_includes_leases"])
        self.assertEqual(nc["total_debt_source"], "vendor_incomplete")
        self.assertIn("INCLUDES", nc["basis"])
        # cash_st/lt_inv both absent here -> 0.0; net = 0 - total_debt.
        self.assertAlmostEqual(nc["net"], -112_756_000_000.0)
        # The shipped defect double-subtracted leases into net_incl_leases
        # (-127,347,000,000.0); the fix must equal net_incl_leases == net.
        self.assertAlmostEqual(nc["net_incl_leases"], nc["net"])
        self.assertNotAlmostEqual(nc["net_incl_leases"], -127_347_000_000.0)

    def test_google_real_shape_lease_inclusive_debt_full_regression(self):
        # D4 acceptance: the EXACT real GOOG balance-sheet shape (archived
        # bundle, 2026-08-07-refresh). Regression pin for the corrected
        # net/net_incl_leases (the shipped defect double-subtracted leases:
        # net_incl_leases would have been 246,588,000,000.0).
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "55911000000",
            "shortTermInvestments": "186563000000",
            "longTermInvestments": "131461000000",
            "shortTermDebt": None,
            "longTermDebt": "98165000000",
            "capitalLeaseObligations": "14591000000",
            "cashAndShortTermInvestments": "55911000000",
            "shortLongTermDebtTotal": "112756000000",
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["cash_st"], 242_474_000_000.0)
        self.assertAlmostEqual(nc["lt_inv"], 131_461_000_000.0)
        self.assertAlmostEqual(nc["total_debt"], 112_756_000_000.0)
        self.assertTrue(nc["total_debt_includes_leases"])
        self.assertAlmostEqual(nc["net"], 261_179_000_000.0)
        # CORRECTED: net_incl_leases == net (leases already inside total_debt).
        self.assertAlmostEqual(nc["net_incl_leases"], 261_179_000_000.0)
        # The shipped (buggy, double-subtracted) value must NEVER reappear.
        self.assertNotAlmostEqual(nc["net_incl_leases"], 246_588_000_000.0)

    def test_pltr_both_absent_lease_only_debt_not_double_subtracted(self):
        # D4: PLTR's real shape -- shortTermDebt AND longTermDebt BOTH
        # absent, and the vendor shortLongTermDebtTotal happens to equal
        # capitalLeaseObligations EXACTLY (the company carries no financial
        # debt besides leases). The "both absent" fallback adopts the vendor
        # aggregate as total_debt (unchanged), but it is 100% lease -- so
        # net_incl_leases must not subtract those same leases again.
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "2291631000",
            "shortTermInvestments": "5734782000",
            "capitalLeaseObligations": "211977000",
            "cashAndShortTermInvestments": "2291631000",
            "shortLongTermDebtTotal": "211977000",
            # shortTermDebt/longTermDebt both absent.
        }
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["total_debt"], 211_977_000.0)
        self.assertTrue(nc["total_debt_includes_leases"])
        self.assertEqual(nc["total_debt_source"], "vendor_both_absent")
        self.assertAlmostEqual(nc["cash_st"], 8_026_413_000.0)
        self.assertAlmostEqual(nc["net"], 7_814_436_000.0)
        # CORRECTED: net_incl_leases == net, NOT the shipped double-subtracted
        # 7,602,459,000.0.
        self.assertAlmostEqual(nc["net_incl_leases"], 7_814_436_000.0)
        self.assertNotAlmostEqual(nc["net_incl_leases"], 7_602_459_000.0)

    def test_partial_components_with_vendor_absent_uses_visible_partial_sum(self):
        # No vendor number exists to fall back to -- the visible component is
        # all we have; this must NOT be confused with the incomplete-FALLBACK
        # path (there is nothing to fall back TO). D4: this collapse must
        # still be disclosed honestly (it silently UNDERSTATES debt), never
        # mislabeled as if a vendor fallback occurred.
        from scripts import build_snapshot as bs
        bal = {"shortTermDebt": "500000000", "longTermDebt": None}
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["total_debt"], 500_000_000.0)
        self.assertEqual(nc["total_debt_source"], "incomplete_no_vendor")
        self.assertIn("no vendor", nc["basis"].lower())
        self.assertFalse(nc["total_debt_includes_leases"])

    def test_partial_components_vendor_not_exceeding_uses_partial_sum(self):
        # Vendor aggregate present but does NOT materially exceed the visible
        # partial sum -- not the measured failure pattern; stay with the
        # partial component sum rather than a fallback that buys nothing.
        from scripts import build_snapshot as bs
        bal = {"shortTermDebt": "500000000", "longTermDebt": None,
               "shortLongTermDebtTotal": "500000000"}
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["total_debt"], 500_000_000.0)
        self.assertNotIn("INCOMPLETE", nc["basis"])
        self.assertEqual(nc["total_debt_source"], "components")


class TestD5LongTermInvestmentsDisclosure(unittest.TestCase):
    """D5: the ``or 0.0`` on longTermInvestments is the exact defect class
    QC4 exists to eliminate -- it is the one net-cash leg with NO raw
    component key recorded and no basis note, so an ABSENT
    longTermInvestments silently becomes a fabricated genuine zero.
    long_term_investments must now be a first-class disclosed component
    (None when absent, distinguishing it from a real 0.0), and its
    treatment must be stated in basis."""

    def test_unh_real_shape_absent_long_term_investments_disclosed(self):
        # Real UNH balance sheet (archived bundle, 2026-08-02): longTermInvestments
        # key IS present but its value is the vendor's null-ish "None" string,
        # so num() correctly yields None -- but the pre-fix code then folded
        # that absence into a fabricated 0.0 with NO trace left behind.
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "31468000000",
            "shortTermInvestments": None,
            "longTermInvestments": "None",
            "shortTermDebt": "3827000000",
            "longTermDebt": None,
            "cashAndShortTermInvestments": "31468000000",
            "shortLongTermDebtTotal": "73328000000",
        }
        nc = bs._build_net_cash(bal)
        # Arithmetic is UNCHANGED (still 0.0 -- we cannot fabricate a real
        # figure), but the raw component is now visibly None, not silently
        # folded away.
        self.assertEqual(nc["lt_inv"], 0.0)
        self.assertIsNone(nc["long_term_investments"])
        self.assertIn("long_term_investments", nc["basis"])
        self.assertIn("absent", nc["basis"].lower())
        # Real measured UNH net: -41.86B (matches the review's finding).
        self.assertAlmostEqual(nc["net"], -41_860_000_000.0)

    def test_genuine_zero_long_term_investments_is_not_confused_with_absent(self):
        # A genuinely-reported zero must be preserved AS a disclosed zero,
        # never silently indistinguishable from "absent".
        from scripts import build_snapshot as bs
        bal = {
            "cashAndCashEquivalentsAtCarryingValue": "1000000000",
            "longTermInvestments": "0",
            "shortTermDebt": "0",
            "longTermDebt": "0",
        }
        nc = bs._build_net_cash(bal)
        self.assertEqual(nc["lt_inv"], 0.0)
        self.assertEqual(nc["long_term_investments"], 0.0)
        self.assertIsNotNone(nc["long_term_investments"])
        self.assertNotIn("long_term_investments", nc["basis"])

    def test_present_nonzero_long_term_investments_is_disclosed_and_used(self):
        from scripts import build_snapshot as bs
        bal = {"longTermInvestments": "84118000000"}
        nc = bs._build_net_cash(bal)
        self.assertAlmostEqual(nc["lt_inv"], 84_118_000_000.0)
        self.assertAlmostEqual(nc["long_term_investments"], 84_118_000_000.0)
        self.assertNotIn("long_term_investments", nc["basis"])


# --------------------------------------------------------------------------- #
# Change 1a: stooq-CSV daily series (no-Alpha-Vantage fallback).
# --------------------------------------------------------------------------- #

class TestStooqCsvDaily(unittest.TestCase):
    """A stooq CSV daily export (bare .csv OR {"result": csv}) parses into the
    standard row shape with adjusted_close == close, and the technicals block
    discloses the convention via series_source. The AV-JSON path is unchanged."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_parse_daily_rows_from_bare_csv_ascending_adj_eq_close(self):
        # Pure-function contract: bare CSV string -> ascending rows, adj == close.
        from scripts import build_snapshot as bs
        csv_text = _stooq_csv(self.b_rows())
        rows = bs.parse_daily_rows(csv_text)
        dates = [r["date"] for r in rows]
        self.assertEqual(dates, sorted(dates))  # ascending
        for r in rows:
            self.assertIsNotNone(r["adjusted_close"])
            self.assertEqual(r["adjusted_close"], r["close"])

    def test_parse_daily_rows_from_result_wrapped_csv(self):
        from scripts import build_snapshot as bs
        wrapped = {"result": _stooq_csv(self.b_rows())}
        rows = bs.parse_daily_rows(wrapped)
        self.assertTrue(len(rows) > 0)
        self.assertEqual(rows[0]["adjusted_close"], rows[0]["close"])

    def b_rows(self):
        return _walk(60, seed=7, start=100.0)

    def test_bare_csv_bundle_sets_series_source_label(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview()
        b.add_daily_csv_bare(); b.add_spy_csv_bare()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        snap = self._build(b)
        self.assertEqual(snap["technicals"]["series_source"],
                         "stooq_csv_close_as_adjusted")
        # rows parsed and technicals computed from them.
        self.assertEqual(snap["technicals"]["ohlcv_rows"], 320)
        self.assertIsNotNone(snap["technicals"]["ma50"])

    def test_result_wrapped_csv_bundle_sets_series_source_label(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview()
        b.add_daily_csv_wrapped(); b.add_spy_csv_wrapped()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        snap = self._build(b)
        self.assertEqual(snap["technicals"]["series_source"],
                         "stooq_csv_close_as_adjusted")

    def test_csv_daily_adjusted_close_equals_close_in_snapshot(self):
        # rv/ma computed off adjusted_close; with adj == close the technicals
        # match the AV path's math when the same walk feeds both shapes.
        b_csv = BundleBuilder(self.dir)
        b_csv.add_global_quote(); b_csv.add_overview()
        b_csv.add_daily_csv_bare(); b_csv.add_spy_csv_bare()
        b_csv.add_income(); b_csv.add_balance(); b_csv.add_cashflow(); b_csv.add_earnings()
        snap_csv = self._build(b_csv)

        d2 = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d2, True)
        b_json = BundleBuilder(d2)
        b_json.add_global_quote(); b_json.add_overview()
        b_json.add_daily(); b_json.add_spy()
        b_json.add_income(); b_json.add_balance(); b_json.add_cashflow(); b_json.add_earnings()
        b_json.write_manifest()
        proc = _run_build(d2)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(d2, f"snapshot_MU_{AS_OF_DATE}.json")) as fh:
            snap_json = json.load(fh)
        # AV adj == close in the fixture, so ma50 matches across shapes.
        self.assertAlmostEqual(snap_csv["technicals"]["ma50"],
                               snap_json["technicals"]["ma50"], places=6)
        self.assertNotIn("series_source", snap_json["technicals"])


# --------------------------------------------------------------------------- #
# Change 1b: web-transcribed fundamentals (statement data wins; web fills gaps).
# --------------------------------------------------------------------------- #

def _web_fund_payload():
    """A full web_fundamentals transcription with cited per-field sources."""
    return {
        "rev_ttm": 26000.0, "rev_growth_latest_q": 1.0, "gm_ttm": 0.5,
        "om_ttm": 0.25, "nm_ttm": 0.15, "eps_ttm": 6.0,
        "eps_ntm_consensus": 7.5, "fcf_ttm": 6800.0,
        "net_cash_defined": {"cash_st": 9000.0, "lt_inv": 2000.0,
                             "total_debt": 4000.0, "net": 7000.0},
        "roe": 0.28,
        "sources": {
            "rev_ttm": "https://example.com/mu/revenue",
            "eps_ttm": "https://example.com/mu/eps",
            "fcf_ttm": "https://example.com/mu/fcf",
            "net_cash_defined": "https://example.com/mu/balance",
        },
    }


class TestWebFundamentals(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, b, data_mode=None):
        b.write_manifest(data_mode=data_mode)
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_no_statements_web_fills_all_and_lists_them(self):
        # No income/balance/cashflow/earnings; web_fundamentals fills the block.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_web_fundamentals(_web_fund_payload())
        snap = self._build(b, data_mode="web_fallback")
        f = snap["fundamentals"]
        self.assertAlmostEqual(f["rev_ttm"], 26000.0)
        self.assertAlmostEqual(f["gm_ttm"], 0.5)
        self.assertAlmostEqual(f["fcf_ttm"], 6800.0)
        self.assertAlmostEqual(f["eps_ntm_consensus"], 7.5)
        self.assertAlmostEqual(f["net_cash_defined"]["net"], 7000.0)
        wtf = f["web_transcribed_fields"]
        # Every field the statement path could not compute got web-filled.
        for field in ("rev_ttm", "rev_growth_latest_q", "gm_ttm", "om_ttm",
                      "nm_ttm", "fcf_ttm", "eps_ntm_consensus",
                      "net_cash_defined"):
            self.assertIn(field, wtf, f"{field} not disclosed as web-filled")
        # eps_ttm and roe come from overview (statement-side, always present in
        # this fixture), NOT web, so they must not be listed as web-transcribed.
        self.assertNotIn("eps_ttm", wtf)
        self.assertNotIn("roe", wtf)
        self.assertAlmostEqual(f["roe"], 0.28)  # from overview
        self.assertAlmostEqual(f["eps_ttm"], 6.0)  # from overview

    def test_valuation_computes_from_web_filled_fundamentals(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_web_fundamentals(_web_fund_payload())
        snap = self._build(b, data_mode="web_fallback")
        v = snap["valuation"]
        # pe_fwd = last / eps_ntm(7.5); fcf_yield from web fcf_ttm.
        self.assertAlmostEqual(v["pe_fwd"], snap["price"]["last"] / 7.5, places=4)
        self.assertIsNotNone(v["fcf_yield"])

    def test_statements_win_only_null_fields_web_filled(self):
        # Statements present AND web_fundamentals present: statement values win;
        # only fields the statement path returned null for get web-filled.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        b.add_estimates()  # so eps_ntm_consensus IS statement-computed (7.5)
        # web payload disagrees on rev_ttm (statement computes 26000 already) and
        # supplies a roe the overview also has (0.28). Statement/overview win.
        web = _web_fund_payload()
        web["rev_ttm"] = 99999.0  # bogus; must be ignored (statement wins)
        web["eps_ntm_consensus"] = 88888.0  # bogus; statement 7.5 wins
        b.add_web_fundamentals(web)
        snap = self._build(b, data_mode="av_free_degraded")
        f = snap["fundamentals"]
        # statement-computed rev_ttm wins over the web value.
        self.assertAlmostEqual(f["rev_ttm"], 26000.0)
        self.assertNotIn("rev_ttm", f["web_transcribed_fields"])
        # eps_ntm_consensus: statement path already computes 7.5 from the FY row,
        # so web must NOT fill it.
        self.assertAlmostEqual(f["eps_ntm_consensus"], 7.5)
        self.assertNotIn("eps_ntm_consensus", f["web_transcribed_fields"])

    def test_web_fundamentals_source_appended_to_meta_sources(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_web_fundamentals(_web_fund_payload())
        snap = self._build(b, data_mode="web_fallback")
        groups = {s["field_group"]: s for s in snap["meta"]["sources"]}
        self.assertIn("web_fundamentals", groups)
        self.assertEqual(sorted(groups["web_fundamentals"]["covers"]),
                         ["fundamentals", "valuation"])


# --------------------------------------------------------------------------- #
# Change 1c: data_mode passthrough.
# --------------------------------------------------------------------------- #

class TestDataMode(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, data_mode):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        b.write_manifest(data_mode=data_mode)
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_explicit_data_mode_passes_through(self):
        for mode in ("alpha_vantage", "av_free_degraded", "web_fallback"):
            snap = self._build(mode)
            self.assertEqual(snap["meta"]["data_mode"], mode)


# --------------------------------------------------------------------------- #
# Change 1: data_source passthrough (bring-your-own-MCP source abstraction).
# --------------------------------------------------------------------------- #

class TestDataSource(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, data_source):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        b.write_manifest(data_source=data_source)
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_explicit_data_source_passes_through(self):
        for source in ("alphavantage", "mcp:polygon", "stooq+web"):
            snap = self._build(source)
            self.assertEqual(snap["meta"]["data_source"], source)


class TestOptionalMissing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_optionals_null_and_listed(self):
        b = BundleBuilder(self.dir)
        # required only + a couple optionals; deliberately omit news/options/short_interest
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            snap = json.load(fh)
        # options block should be null (no chain)
        self.assertIsNone(snap["options"])
        self.assertIsNone(snap["sentiment"]["put_call_ratio_realtime"])
        self.assertIsNone(snap["sentiment"]["short_interest_pct"])
        self.assertIsNone(snap["macro"]["treasury_10y"])
        for key in ("news_sentiment", "options_chain", "short_interest"):
            self.assertIn(key, snap["meta"]["missing"])

    def test_chain_without_date_falls_back_to_last_ohlcv_date(self):
        # A dateless EOD chain must be stamped with the last trading day,
        # NOT file mtime (which is build day and trips check_options_freshness).
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_chain(with_date=False)
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            snap = json.load(fh)
        self.assertEqual(snap["options"]["chain_as_of"],
                         snap["technicals"]["last_ohlcv_date"])


class TestRequiredMissing(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_missing_overview_exit_2(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_daily(); b.add_spy()  # NO overview
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 2, f"stdout={proc.stdout} stderr={proc.stderr}")
        self.assertIn("overview", (proc.stdout + proc.stderr).lower())


class TestQCGate(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.b = BundleBuilder(self.dir).build_full()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.snap_path = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")

    def test_gate_passes_and_writes_back(self):
        proc = _run_gate(self.snap_path)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        with open(self.snap_path) as fh:
            snap = json.load(fh)
        self.assertIs(snap["meta"]["qc"]["passed"], True)
        # O15 added check_security_master (10th check); QC4 added
        # check_net_cash_vendor_signature (11th, disclosure-only); QC1 added
        # check_forward_pe_crossvendor (12th); QC3 added check_eps (13th) and
        # check_eps_quarterly_shares (14th).
        self.assertTrue(len(snap["meta"]["qc"]["checks"]) == 14)

    def test_gate_fails_on_corrupt_mktcap(self):
        # G1: ratio must be OUTSIDE the multi-class band (0.15, 1.0) to fail.
        # overview × 10 → ratio = computed/(overview×10) ≈ 0.1 < 0.15 → FAIL.
        with open(self.snap_path) as fh:
            snap = json.load(fh)
        snap["price"]["mktcap_overview"] *= 10
        with open(self.snap_path, "w") as fh:
            json.dump(snap, fh)
        proc = _run_gate(self.snap_path)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        with open(self.snap_path) as fh:
            snap2 = json.load(fh)
        self.assertIs(snap2["meta"]["qc"]["passed"], False)

    def test_waive_flips_to_pass(self):
        # G1: same out-of-band mutation as above; waiver must suppress the FAIL.
        with open(self.snap_path) as fh:
            snap = json.load(fh)
        snap["price"]["mktcap_overview"] *= 10
        with open(self.snap_path, "w") as fh:
            json.dump(snap, fh)
        proc = _run_gate(self.snap_path,
                         waivers=["check_mktcap:known share lag"])
        self.assertEqual(proc.returncode, 0, proc.stdout)
        self.assertIn("WAIVED", proc.stdout)


# --------------------------------------------------------------------------- #
# QF2: meta.latest_trading_day + QC note when as_of != latest_trading_day
# --------------------------------------------------------------------------- #

class TestLatestTradingDay(unittest.TestCase):
    """QF2 regression: meta.latest_trading_day populated from Global Quote field
    07. latest trading day; qc_gate emits a non-blocking note when the dates diverge."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build_snap(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh), out

    def test_latest_trading_day_present_when_in_quote(self):
        # BundleBuilder.add_global_quote includes "07. latest trading day".
        b = BundleBuilder(self.dir).build_full()
        snap, _ = self._build_snap(b)
        # The fixture last_date is the last row of the 320-row walk ending at
        # 2026-07-15 (the most recent business day before AS_OF 2026-07-16).
        self.assertEqual(snap["meta"]["latest_trading_day"], b.last_date)

    def test_latest_trading_day_null_when_absent_in_quote(self):
        # Build a bundle whose global_quote lacks "07. latest trading day".
        b = BundleBuilder(self.dir)
        gq_no_ltd = {"Global Quote": {
            "01. symbol": b.ticker,
            "05. price": f"{b.last}",
            "08. previous close": f"{b.stock_rows[-2]['close']}",
            "03. high": f"{b.stock_rows[-1]['high']}",
            "04. low": f"{b.stock_rows[-1]['low']}",
            # NOTE: "07. latest trading day" deliberately absent
        }}
        b.files = {}
        b._add("global_quote", "global_quote.json", gq_no_ltd, "GLOBAL_QUOTE")
        b.add_overview(); b.add_daily(); b.add_spy()
        snap, _ = self._build_snap(b)
        self.assertIsNone(snap["meta"]["latest_trading_day"])

    def test_qc_note_emitted_when_dates_differ(self):
        """Non-blocking QC note when meta.latest_trading_day != as_of date.
        The gate may or may not pass for other reasons; the note itself is
        non-blocking and must always appear in the attestation regardless.
        """
        b = BundleBuilder(self.dir).build_full()
        snap, snap_path = self._build_snap(b)

        # Keep as_of_utc unchanged (2026-07-16); set latest_trading_day to a
        # different (earlier) date to simulate a weekend/stale-print scenario.
        snap["meta"]["latest_trading_day"] = "2026-07-14"
        with open(snap_path, "w") as fh:
            json.dump(snap, fh)

        proc = _run_gate(snap_path)
        # The note is non-blocking: the gate exit code may be 0 or 1 depending
        # on other checks (staleness, etc.) -- we assert on the NOTE itself.
        combined = proc.stdout + proc.stderr
        self.assertIn("2026-07-14", combined,
                      "expected latest_trading_day date in attestation")
        self.assertIn("weekend/stale", combined)

    def test_qc_no_note_when_dates_match(self):
        """No stale-print note when as_of date matches latest_trading_day."""
        b = BundleBuilder(self.dir).build_full()
        snap, snap_path = self._build_snap(b)
        # Force the dates to match.
        snap["meta"]["latest_trading_day"] = AS_OF_DATE
        with open(snap_path, "w") as fh:
            json.dump(snap, fh)
        proc = _run_gate(snap_path)
        self.assertNotIn("weekend/stale", proc.stdout + proc.stderr)


# --------------------------------------------------------------------------- #
# QF3: future_expiries filters expired rows from expected_moves / atm_iv_by_expiry
# --------------------------------------------------------------------------- #

class TestQF3FutureExpiriesInBuild(unittest.TestCase):
    """QF3 regression: build_snapshot must produce no expected_moves or
    atm_iv_by_expiry entries whose expiry < as_of_date."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _add_chain_with_past_expiry(self, b):
        """Chain that includes one PAST expiry (2026-07-01) and two future ones."""
        as_of = AS_OF_DATE  # "2026-07-16"
        def c(exp, k, t, mark, iv, oi):
            return {"expiration": exp, "strike": str(k), "type": t, "mark": str(mark),
                    "implied_volatility": str(iv), "delta": "0.5",
                    "open_interest": str(oi), "volume": "5", "date": as_of}
        chain = [
            # PAST expiry -- must be filtered out
            c("2026-07-01", 100, "put", 0.01, 0.50, 10),
            c("2026-07-01", 100, "call", 0.01, 0.50, 10),
            # future expiries
            c("2026-08-14", 100, "put", 4.0, 0.55, 1000),
            c("2026-08-14", 100, "call", 5.0, 0.50, 900),
            c("2026-09-18", 100, "put", 6.0, 0.52, 400),
            c("2026-09-18", 100, "call", 7.0, 0.47, 500),
        ]
        b._add("options_chain", "chain.json", {"data": chain}, "HISTORICAL_OPTIONS")

    def test_no_past_expiry_in_expected_moves(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        self._add_chain_with_past_expiry(b)
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            snap = json.load(fh)
        opts = snap.get("options") or {}
        for em in opts.get("expected_moves", []):
            self.assertGreaterEqual(
                em["expiry"], AS_OF_DATE,
                f"expired expiry {em['expiry']} found in expected_moves"
            )

    def test_no_past_expiry_in_atm_iv_by_expiry(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        self._add_chain_with_past_expiry(b)
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            snap = json.load(fh)
        opts = snap.get("options") or {}
        for row in opts.get("atm_iv_by_expiry", []):
            self.assertGreaterEqual(
                row["expiry"], AS_OF_DATE,
                f"expired expiry {row['expiry']} found in atm_iv_by_expiry"
            )


# --------------------------------------------------------------------------- #
# QF5: revisions_null_reason populated + loud warning in score_sentiment
# --------------------------------------------------------------------------- #

class TestQF5RevisionsNullReason(unittest.TestCase):
    """QF5 regression: build_fundamentals records WHY revisions_90d is null."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build_snap(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_revisions_null_reason_no_future_fy_row(self):
        """No estimates file -> revisions_null_reason = no_future_fy_row."""
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        # No estimates -> fy = [] -> reason = no_future_fy_row
        snap = self._build_snap(b)
        self.assertIsNone(snap["fundamentals"]["revisions_90d"])
        self.assertEqual(snap["fundamentals"]["revisions_null_reason"], "no_future_fy_row")

    def test_revisions_null_reason_fields_absent(self):
        """FY row present but revision fields absent -> fields_absent reason."""
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        # Add estimates with a future FY row but no revision fields.
        est_no_revisions = [
            {"date": "2027-06-30", "horizon": "fiscal year",
             "eps_estimate_average": "7.50",
             # eps_estimate_average_90_days_ago absent -> pct will be None
             # revision_up/down absent
             "revenue_estimate_average": "34000"},
        ]
        b._add("earnings_estimates", "estimates.json",
               {"symbol": b.ticker, "estimates": est_no_revisions},
               "EARNINGS_ESTIMATES")
        snap = self._build_snap(b)
        # revisions dict IS populated (eps_now has a value) but some fields null
        rev = snap["fundamentals"]["revisions_90d"]
        self.assertIsNotNone(rev)
        self.assertIsNone(rev["pct"])
        self.assertIsNone(rev["up_30d"])
        reason = snap["fundamentals"]["revisions_null_reason"]
        self.assertIsNotNone(reason)
        self.assertIn("future_fy_row_present_but_fields_absent", reason)
        self.assertIn("eps_estimate_average_90_days_ago", reason)

    def test_revisions_null_reason_none_when_all_fields_present(self):
        """When revisions_90d is fully populated, revisions_null_reason is None."""
        b = BundleBuilder(self.dir).build_full()  # includes full estimates
        snap = self._build_snap(b)
        self.assertIsNotNone(snap["fundamentals"]["revisions_90d"])
        self.assertIsNone(snap["fundamentals"]["revisions_null_reason"])


# --------------------------------------------------------------------------- #
# Wave 2 A1: event-aware deterministic snapshot fields (schema 0.3.0).
# --------------------------------------------------------------------------- #

def _expected_move(rows, report_date):
    """Reference implementation of the A1 reaction-window convention.

    move_pct = close[first trading day >= D+1] / close[last trading day <= D-1] - 1,
    or None if OHLCV is missing on either side. ``rows`` are the fixture rows
    (date/close), oldest-first.
    """
    import datetime as _dt
    d = _dt.date.fromisoformat(report_date)
    before = (d - _dt.timedelta(days=1)).isoformat()
    after = (d + _dt.timedelta(days=1)).isoformat()
    pre = [r["close"] for r in rows if r["date"] <= before]
    post = [r["close"] for r in rows if r["date"] >= after]
    if not pre or not post:
        return None
    return post[0] / pre[-1] - 1


class TestA1EventAwareFields(unittest.TestCase):
    """Deterministic event/tail fields added to the snapshot in Wave 2 A1."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_days_to_event(self):
        # next earnings 2026-09-25; as_of 2026-07-16 -> 71 calendar days.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        import datetime as _dt
        expected = (_dt.date(2026, 9, 25) - _dt.date(2026, 7, 16)).days
        self.assertEqual(snap["events"]["days_to_event"], expected)

    def test_days_to_event_null_when_no_next_earnings(self):
        # No earnings_calendar file -> next_earnings null -> days_to_event null.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        snap = self._build(b)
        self.assertIsNone(snap["events"]["next_earnings"])
        self.assertIsNone(snap["events"]["days_to_event"])

    def test_implied_move_copied_from_sentiment(self):
        # events.implied_move must be byte-identical to the sentiment value
        # (reused, not recomputed).
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        self.assertIsNotNone(snap["sentiment"]["implied_move_next_earnings_pct"])
        self.assertEqual(snap["events"]["implied_move"],
                         snap["sentiment"]["implied_move_next_earnings_pct"])

    def test_implied_move_null_when_no_chain(self):
        # No options chain -> sentiment implied move null -> events.implied_move null.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_earnings_calendar()
        snap = self._build(b)
        self.assertIsNone(snap["sentiment"]["implied_move_next_earnings_pct"])
        self.assertIsNone(snap["events"]["implied_move"])

    def test_earnings_move_history_hand_computed(self):
        # Fixture earnings has 4 reportedDates within the daily-row range; each
        # move must match the documented reaction-window convention exactly.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        emh = snap["events"]["earnings_move_history"]
        # The 4 fixture reportedDates (newest-first, matching earn_q order).
        report_dates = ["2026-07-01", "2026-04-01", "2026-01-05", "2025-10-05"]
        self.assertEqual([m["reported_date"] for m in emh], report_dates)
        for m in emh:
            expected = _expected_move(b.stock_rows, m["reported_date"])
            self.assertIsNotNone(expected)
            self.assertAlmostEqual(m["move_pct"], expected, places=9)

    def test_earnings_move_history_key_is_reported_date_not_fiscal_date_ending(self):
        # QC7: the emitted key/value must be AV's reportedDate, never
        # fiscalDateEnding (MU's off-calendar fiscal quarters make the two
        # diverge -- a mis-join trap). The build_full fixture's rows already
        # supply BOTH, distinct, per quarter (fiscalDateEnding 2026-06-30 vs
        # reportedDate 2026-07-01) -- pin the rename as semantically correct.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        emh = snap["events"]["earnings_move_history"]
        self.assertIn("reported_date", emh[0])
        self.assertNotIn("quarter_end", emh[0])
        self.assertEqual(emh[0]["reported_date"], "2026-07-01")
        self.assertNotEqual(emh[0]["reported_date"], "2026-06-30")  # fiscalDateEnding

    def test_earnings_move_history_skips_out_of_range_quarter(self):
        # A reportedDate BEFORE the daily-row range (no pre-window close) is
        # skipped; the other quarters remain, capped at 8, newest-first.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_chain(); b.add_earnings_calendar()
        # Earnings: 3 in-range dates + one date far before the walk start
        # (2020-01-15, no pre/post rows) which MUST be skipped.
        q = [
            {"fiscalDateEnding": "2026-06-30", "reportedDate": "2026-07-01", "reportedEPS": "1.60"},
            {"fiscalDateEnding": "2026-03-31", "reportedDate": "2026-04-01", "reportedEPS": "1.40"},
            {"fiscalDateEnding": "2025-12-31", "reportedDate": "2026-01-05", "reportedEPS": "1.20"},
            {"fiscalDateEnding": "2019-12-31", "reportedDate": "2020-01-15", "reportedEPS": "0.50"},
        ]
        b._add("earnings", "earnings.json",
               {"symbol": b.ticker, "annualEarnings": [], "quarterlyEarnings": q},
               "EARNINGS")
        snap = self._build(b)
        emh = snap["events"]["earnings_move_history"]
        # The out-of-range quarter (2020-01-15) is dropped; 3 remain.
        self.assertEqual([m["reported_date"] for m in emh],
                         ["2026-07-01", "2026-04-01", "2026-01-05"])
        for m in emh:
            expected = _expected_move(b.stock_rows, m["reported_date"])
            self.assertAlmostEqual(m["move_pct"], expected, places=9)

    def test_earnings_move_history_skips_quarter_missing_reported_date(self):
        # A quarter with no reportedDate is skipped (no key error, no entry).
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_chain(); b.add_earnings_calendar()
        q = [
            {"fiscalDateEnding": "2026-06-30", "reportedEPS": "1.60"},  # no reportedDate
            {"fiscalDateEnding": "2026-03-31", "reportedDate": "2026-04-01", "reportedEPS": "1.40"},
        ]
        b._add("earnings", "earnings.json",
               {"symbol": b.ticker, "annualEarnings": [], "quarterlyEarnings": q},
               "EARNINGS")
        snap = self._build(b)
        emh = snap["events"]["earnings_move_history"]
        self.assertEqual([m["reported_date"] for m in emh], ["2026-04-01"])

    def test_earnings_move_history_empty_when_no_earnings(self):
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        snap = self._build(b)
        self.assertEqual(snap["events"]["earnings_move_history"], [])

    def test_implied_move_vs_own_history_pctile(self):
        # implied_move (~0.127) exceeds all 4 abs history moves (<0.012) ->
        # 100th percentile. Hand-check against the same rule the builder uses.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        ev = snap["events"]
        im = ev["implied_move"]
        abs_moves = [abs(m["move_pct"]) for m in ev["earnings_move_history"]]
        expected = 100 * sum(1 for a in abs_moves if a <= im) / len(abs_moves)
        self.assertAlmostEqual(ev["implied_move_vs_own_history_pctile"], expected,
                               places=9)

    def test_implied_move_vs_own_history_pctile_null_without_implied(self):
        # No chain -> implied_move null -> pctile null (one input absent).
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_earnings(); b.add_earnings_calendar()
        snap = self._build(b)
        self.assertIsNone(snap["events"]["implied_move"])
        self.assertIsNone(snap["events"]["implied_move_vs_own_history_pctile"])

    def test_overnight_gap_block_shape_and_values(self):
        # overnight_gap block computed off the fixture's open + adjusted_close.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        og = snap["technicals"]["overnight_gap"]
        self.assertIsNotNone(og)
        self.assertEqual(set(og),
                         {"mean_abs", "p95_abs", "max_abs", "excess_kurtosis",
                          "jump_count_2sigma", "n", "window_years_scored",
                          "p95_abs_3y", "tail_mean_95_3y", "n_3y"})
        # n gaps == len(rows) - 1 (fixture has open + adj_close on every row).
        self.assertEqual(og["n"], len(b.stock_rows) - 1)
        # Cross-check against the indicator library over the same series.
        import math
        from scripts import indicators
        rows = [{"open": r["open"], "adjusted_close": r["adj"]} for r in b.stock_rows]
        gaps = indicators.overnight_gap_series(rows)
        abs_gaps = sorted(abs(g) for g in gaps)
        self.assertAlmostEqual(og["mean_abs"], sum(abs_gaps) / len(abs_gaps), places=9)
        self.assertAlmostEqual(og["max_abs"], max(abs_gaps), places=9)
        self.assertAlmostEqual(og["excess_kurtosis"],
                               indicators.excess_kurtosis(gaps), places=9)
        self.assertEqual(og["jump_count_2sigma"],
                         indicators.jump_count_2sigma(gaps))
        # Trailing-3y scoring window (O1): the fixture has < 3y of rows, so it equals
        # the full series -> p95_abs_3y == p95_abs, and tail_mean_95_3y = mean of the
        # worst-5% |gaps| (nearest-rank p95 index).
        self.assertEqual(og["window_years_scored"], 3)
        self.assertEqual(og["n_3y"], og["n"])
        self.assertAlmostEqual(og["p95_abs_3y"], og["p95_abs"], places=9)
        idx = min(len(abs_gaps) - 1, max(0, math.ceil(0.95 * len(abs_gaps)) - 1))
        worst = abs_gaps[idx:]
        self.assertAlmostEqual(og["tail_mean_95_3y"], sum(worst) / len(worst), places=9)

    def test_schema_version_is_0_4_0(self):
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        self.assertEqual(snap["meta"]["schema_version"], "0.5.0")


# --------------------------------------------------------------------------- #
# Wave 3A: sentiment positioning dynamics — snapshot DATA layer.
# news_heat (EWMA half-life 3d + volume z), dtc, skew promotion, insider CMP.
# --------------------------------------------------------------------------- #
class TestWave3ANewsHeat(unittest.TestCase):
    """_news_heat: relevance-and-decay-weighted EWMA of ticker_sentiment_score."""

    def test_ewma_hand_computed_half_life_3(self):
        from scripts import build_snapshot as bs
        # Two MU articles: age 0 (score +1.0) and age 3 (score -1.0), both
        # relevance 1.0. Half-life 3 -> weights 1.0 and 0.5.
        # ewma = (1.0*1.0 + (-1.0)*0.5)/(1.0+0.5) = 1/3.
        news = {"feed": [
            {"time_published": "20260716T120000", "ticker_sentiment": [
                {"ticker": "MU", "ticker_sentiment_score": "1.0", "relevance_score": "1.0"}]},
            {"time_published": "20260713T120000", "ticker_sentiment": [
                {"ticker": "MU", "ticker_sentiment_score": "-1.0", "relevance_score": "1.0"}]},
            # A non-MU article is skipped (does not mention the ticker).
            {"time_published": "20260710T120000", "ticker_sentiment": [
                {"ticker": "AAPL", "ticker_sentiment_score": "0.9", "relevance_score": "1.0"}]},
        ]}
        nh = bs._news_heat(news, "2026-07-16", "MU")
        self.assertEqual(nh["half_life_days"], 3)
        self.assertEqual(nh["n_articles"], 2)      # only MU-mentioning articles
        self.assertAlmostEqual(nh["ewma"], 1.0 / 3.0, places=12)

    def test_relevance_scales_weight(self):
        from scripts import build_snapshot as bs
        # Same age (0) so no decay; the +1 article has relevance 0.25, the -1 has
        # relevance 1.0. ewma = (1*0.25 + (-1)*1.0)/(0.25+1.0) = -0.75/1.25 = -0.6.
        news = {"feed": [
            {"time_published": "20260716T090000", "ticker_sentiment": [
                {"ticker": "MU", "ticker_sentiment_score": "1.0", "relevance_score": "0.25"}]},
            {"time_published": "20260716T090000", "ticker_sentiment": [
                {"ticker": "MU", "ticker_sentiment_score": "-1.0", "relevance_score": "1.0"}]},
        ]}
        nh = bs._news_heat(news, "2026-07-16", "MU")
        self.assertAlmostEqual(nh["ewma"], -0.6, places=12)

    def test_null_when_feed_absent_or_no_match(self):
        from scripts import build_snapshot as bs
        self.assertIsNone(bs._news_heat(None, "2026-07-16", "MU"))
        self.assertIsNone(bs._news_heat({}, "2026-07-16", "MU"))
        self.assertIsNone(bs._news_heat({"feed": []}, "2026-07-16", "MU"))
        # feed present but no article mentions the ticker -> null block.
        news = {"feed": [
            {"time_published": "20260716T120000", "ticker_sentiment": [
                {"ticker": "AAPL", "ticker_sentiment_score": "0.5", "relevance_score": "1.0"}]}]}
        self.assertIsNone(bs._news_heat(news, "2026-07-16", "MU"))
        self.assertIsNone(bs._news_heat(news, "2026-07-16", None))  # no ticker

    def test_volume_z_null_below_five_days_then_computed(self):
        from scripts import build_snapshot as bs
        # < 5 distinct article dates -> volume_z is None.
        def art(day, score="0.1"):
            return {"time_published": f"2026070{day}T120000", "ticker_sentiment": [
                {"ticker": "MU", "ticker_sentiment_score": score, "relevance_score": "1.0"}]}
        few = {"feed": [art(1), art(2), art(3)]}
        self.assertIsNone(bs._news_heat(few, "2026-07-09", "MU")["volume_z"])
        # >= 5 distinct days AND varying per-day counts (non-zero stdev) -> the
        # volume_z is a number: a recent 2-day spike vs the sparser earlier days.
        many = {"feed": [art(1), art(2), art(3), art(5),
                         art(8), art(8), art(9), art(9), art(9)]}
        nh = bs._news_heat(many, "2026-07-09", "MU")
        self.assertIsNotNone(nh["volume_z"])
        self.assertGreater(nh["volume_z"], 0)   # trailing 3d is a positive spike

    def test_full_bundle_news_heat_null_when_feed_lacks_ticker_sentiment(self):
        # The standard build_full news fixture carries no ticker_sentiment ->
        # news_heat degrades to a null block (n_articles would be 0).
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        b = BundleBuilder(d).build_full()
        proc = _run_build(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.load(open(os.path.join(d, f"snapshot_MU_{AS_OF_DATE}.json")))
        self.assertIsNone(snap["sentiment"]["news_heat"])


class TestWave3ADtcAndSkew(unittest.TestCase):
    """dtc formula + null guards, and skew_25d_30d promotion into sentiment."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_dtc_formula_exact(self):
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        s = snap["sentiment"]
        p = snap["price"]
        # dtc (O1 basis) = (si%/100 * base_shares_m*1e6) / adv_shares_3m, where the base
        # is SharesFloat if present else SharesOutstanding (== shares_diluted_m), and the
        # denominator is the DIRECT 3m share ADV (not adv_dollar_3m/last).
        base_m = p.get("shares_float_m") or p["shares_diluted_m"]
        expected = (s["short_interest_pct"] / 100.0) * base_m * 1e6 / p["adv_shares_3m"]
        self.assertAlmostEqual(s["dtc"], expected, places=6)
        self.assertIn(s["dtc_basis"], ("float", "shares_outstanding"))

    def test_dtc_null_when_short_interest_absent(self):
        # No short_interest file -> si% null -> dtc null.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_chain(); b.add_pc()
        snap = self._build(b)
        self.assertIsNone(snap["sentiment"]["short_interest_pct"])
        self.assertIsNone(snap["sentiment"]["dtc"])

    def test_skew_promoted_into_sentiment(self):
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        # sentiment.skew_25d_30d is the SAME value as options.skew_25d_30d.
        self.assertIsNotNone(snap["options"]["skew_25d_30d"])
        self.assertEqual(snap["sentiment"]["skew_25d_30d"],
                         snap["options"]["skew_25d_30d"])

    def test_skew_null_when_no_options(self):
        # No chain -> options block None -> sentiment.skew_25d_30d null.
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        snap = self._build(b)
        self.assertIsNone(snap["options"])
        self.assertIsNone(snap["sentiment"]["skew_25d_30d"])


class TestWave4BEventVolAndExEarningsRV(unittest.TestCase):
    """Wave 4B: options.event_vol + options.rv20_ex_earnings snapshot fields."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _build(self, b):
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        out = os.path.join(self.dir, f"snapshot_MU_{AS_OF_DATE}.json")
        with open(out) as fh:
            return json.load(fh)

    def test_event_vol_present_and_brackets_earnings(self):
        # Fixture: earnings 2026-09-25; chain expiries 2026-08-14/09-18/10-16.
        # pre = 2026-09-18 (last < earnings), post = 2026-10-16 (first >= earnings).
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        ev = snap["options"]["event_vol"]
        self.assertIsNotNone(ev)
        self.assertEqual(ev["exp_pre"], "2026-09-18")
        self.assertEqual(ev["exp_post"], "2026-10-16")
        # ATM IVs (call/put mean at K=100): pre (0.47+0.52)/2, post (0.45+0.50)/2.
        self.assertAlmostEqual(ev["iv_pre"], (0.47 + 0.52) / 2)
        self.assertAlmostEqual(ev["iv_post"], (0.45 + 0.50) / 2)
        self.assertGreater(ev["event_implied_move"], 0.0)
        # Desk variance additivity with both horizons from the snapshot's as_of.
        from datetime import date
        def _d(s):
            y, m, dd = str(s)[:10].split("-"); return date(int(y), int(m), int(dd))
        aod = _d(snap["meta"]["as_of_utc"])
        t_pre = (_d("2026-09-18") - aod).days / 365.0
        t_post = (_d("2026-10-16") - aod).days / 365.0
        expected = math.sqrt(ev["iv_post"] ** 2 * t_post - ev["iv_pre"] ** 2 * t_pre)
        self.assertAlmostEqual(ev["event_implied_move"], expected, places=10)

    def test_rv20_ex_earnings_present(self):
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        rv_ex = snap["options"]["rv20_ex_earnings"]
        self.assertIsNotNone(rv_ex)
        self.assertGreaterEqual(rv_ex, 0.0)

    def test_iv_gate_uses_ex_earnings_rv(self):
        # Code-review fix: the PRIMARY IV-vs-realized gate compares iv30 against
        # the CLEANER ex-earnings RV. rv20_for_iv_comparison is the RV actually
        # used; it equals rv20_ex_earnings when present, else the contaminated
        # rv20_ann. iv_minus_rv20 must be computed off that same RV. The
        # contaminated rv20_ann is retained as a disclosed field, and
        # rv20_ex_earnings_stripped flags whether a print was actually masked.
        b = BundleBuilder(self.dir).build_full()
        snap = self._build(b)
        o = snap["options"]
        used = o["rv20_for_iv_comparison"]
        if o["rv20_ex_earnings"] is not None:
            self.assertEqual(used, o["rv20_ex_earnings"])
        else:
            self.assertEqual(used, o["rv20_ann"])
        self.assertIn("rv20_ann", o)
        self.assertIn("rv20_ex_earnings_stripped", o)
        # stripped flag true iff the two RVs materially differ.
        if o["rv20_ex_earnings"] is not None and o["rv20_ann"] is not None:
            differ = abs(o["rv20_ex_earnings"] - o["rv20_ann"]) > 1e-9
            self.assertEqual(o["rv20_ex_earnings_stripped"], differ)

    def test_event_vol_and_rv_ex_null_when_no_chain(self):
        # No chain -> options block None (both fields unreachable, block is null).
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_earnings_calendar()
        snap = self._build(b)
        self.assertIsNone(snap["options"])

    def test_event_vol_null_when_no_earnings_date(self):
        # Chain present but NO earnings_calendar -> next_earnings.date None ->
        # event_vol null. rv20_ex_earnings still computes (masks nothing).
        b = BundleBuilder(self.dir)
        b.add_global_quote(); b.add_overview(); b.add_daily(); b.add_spy()
        b.add_chain(); b.add_pc()
        snap = self._build(b)
        self.assertIsNotNone(snap["options"])
        self.assertIsNone(snap["options"]["event_vol"])
        # rv20_ex_earnings is still present (no earnings quarters to mask, but the
        # RV over the daily series still computes).
        self.assertIsNotNone(snap["options"]["rv20_ex_earnings"])


class TestWave3AInsiderClassification(unittest.TestCase):
    """Cohen/Malloy/Pomorski routine-vs-opportunistic classifier + graceful degrade."""

    def test_active_with_36_month_history(self):
        from scripts import build_snapshot as bs
        # >= 24mo history. ROUT sells every July (2023/24/25 history) -> the
        # 2026-07 sale is ROUTINE. OPP1 + OPP2 both SELL within 30d, no prior-July
        # history -> OPPORTUNISTIC cluster. as_of 2026-07-30 so all 2026 rows are
        # within the trailing 90d.
        data = [
            {"transaction_date": "2023-07-10", "executive": "ROUT",
             "acquisition_or_disposal": "D", "shares": "100", "share_price": "50"},
            {"transaction_date": "2024-07-12", "executive": "ROUT",
             "acquisition_or_disposal": "D", "shares": "100", "share_price": "55"},
            {"transaction_date": "2025-07-11", "executive": "ROUT",
             "acquisition_or_disposal": "D", "shares": "100", "share_price": "60"},
            {"transaction_date": "2026-07-05", "executive": "ROUT",
             "acquisition_or_disposal": "D", "shares": "100", "share_price": "65"},
            {"transaction_date": "2026-07-01", "executive": "OPP1",
             "acquisition_or_disposal": "D", "shares": "200", "share_price": "64"},
            {"transaction_date": "2026-07-20", "executive": "OPP2",
             "acquisition_or_disposal": "D", "shares": "300", "share_price": "66"},
        ]
        ic = bs.build_insider_classification({"data": data}, "2026-07-30")
        self.assertTrue(ic["classifier_active"])
        self.assertEqual(ic["history_months"], 36)     # 2023-07 -> 2026-07
        self.assertEqual(ic["n_insiders"], 3)
        # ROUT's July 2026 sale tagged routine: routine_net = -100*65 = -6500.
        self.assertAlmostEqual(ic["routine_net_usd"], -6500.0)
        # Opportunistic net = -200*64 + -300*66 = -32600.
        self.assertAlmostEqual(ic["opportunistic_net_usd"], -32600.0)
        # OPP1 + OPP2 same side (D) within 30d -> cluster.
        self.assertTrue(ic["opportunistic_cluster"])

    def test_no_cluster_when_single_opportunistic_insider(self):
        from scripts import build_snapshot as bs
        # >=24mo history but only ONE opportunistic insider -> no cluster.
        data = [
            {"transaction_date": "2023-03-10", "executive": "A",
             "acquisition_or_disposal": "A", "shares": "100", "share_price": "50"},
            {"transaction_date": "2026-07-01", "executive": "SOLO",
             "acquisition_or_disposal": "D", "shares": "200", "share_price": "64"},
        ]
        ic = bs.build_insider_classification({"data": data}, "2026-07-30")
        self.assertTrue(ic["classifier_active"])
        self.assertFalse(ic["opportunistic_cluster"])

    def test_graceful_degrade_below_24_months(self):
        from scripts import build_snapshot as bs
        # A 63-day span (< 24mo) -> classifier inactive, splits + cluster null.
        data = [
            {"transaction_date": "2026-05-01", "executive": "X",
             "acquisition_or_disposal": "A", "shares": "100", "share_price": "50"},
            {"transaction_date": "2026-07-03", "executive": "Y",
             "acquisition_or_disposal": "D", "shares": "40", "share_price": "60"},
        ]
        ic = bs.build_insider_classification({"data": data}, "2026-07-16")
        self.assertFalse(ic["classifier_active"])
        self.assertEqual(ic["history_months"], 2)   # May -> July
        self.assertIsNone(ic["opportunistic_cluster"])
        self.assertIsNone(ic["opportunistic_net_usd"])
        self.assertIsNone(ic["routine_net_usd"])
        self.assertEqual(ic["n_insiders"], 2)

    def test_null_block_when_no_priced_rows(self):
        from scripts import build_snapshot as bs
        self.assertIsNone(bs.build_insider_classification(None, "2026-07-16"))
        self.assertIsNone(bs.build_insider_classification({"data": []}, "2026-07-16"))
        # Only RSU (blank price) rows -> no priced rows -> null block.
        rsu = {"data": [{"transaction_date": "2026-07-01", "executive": "Z",
                         "acquisition_or_disposal": "A", "shares": "500",
                         "share_price": ""}]}
        self.assertIsNone(bs.build_insider_classification(rsu, "2026-07-16"))

    def test_full_bundle_insider_classification_graceful(self):
        # The standard build_full insider fixture spans < 24 months -> the
        # snapshot carries a graceful (inactive) classification block.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        b = BundleBuilder(d).build_full()
        proc = _run_build(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.load(open(os.path.join(d, f"snapshot_MU_{AS_OF_DATE}.json")))
        ic = snap["sentiment"]["insider_classification"]
        self.assertIsNotNone(ic)
        self.assertFalse(ic["classifier_active"])
        self.assertIsNone(ic["opportunistic_net_usd"])


# --------------------------------------------------------------------------- #
# Wave 4A: technical regime + institutional levels (build_technicals fields)
# --------------------------------------------------------------------------- #

def _ohlcv(closes, dates=None, vol=1_000_000, range_pct=0.01):
    """OHLCV rows from a close series (oldest-first). high/low bracket close by
    ``range_pct``; adjusted_close == close (a clean unadjusted fixture)."""
    import datetime as _dt
    if dates is None:
        day = _dt.date(2026, 7, 15)
        dates = []
        while len(dates) < len(closes):
            if day.weekday() < 5:
                dates.append(day.isoformat())
            day -= _dt.timedelta(days=1)
        dates.reverse()
    rows = []
    for i, c in enumerate(closes):
        rows.append({
            "date": dates[i], "open": c, "high": c * (1 + range_pct),
            "low": c * (1 - range_pct), "close": c,
            "adjusted_close": float(c), "volume": vol,
        })
    return rows


class TestWave4ATechnicals(unittest.TestCase):
    """Wave 4A additive fields on build_technicals: adx14, stage, ad_line_slope,
    upvol_ratio, vwap_52wk_high, vwap_earnings. All pure-OHLCV + null-safe."""

    def test_new_fields_present_and_typed(self):
        from scripts import build_snapshot as bs
        # A 300-row uptrend -> every new field computes.
        closes = [100.0 * (1.003 ** i) for i in range(300)]
        rows = _ohlcv(closes)
        block = bs.build_technicals(rows)
        for key in ("adx14", "stage", "ad_line_slope", "upvol_ratio",
                    "vwap_52wk_high", "vwap_earnings"):
            self.assertIn(key, block)
        self.assertIsInstance(block["adx14"], float)
        self.assertIn(block["stage"], (1, 2, 3, 4))
        self.assertIsInstance(block["ad_line_slope"], float)
        self.assertTrue(0.0 <= block["upvol_ratio"] <= 1.0)
        self.assertIsInstance(block["vwap_52wk_high"], float)

    def test_adx14_matches_indicator(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        closes = [100.0 * (1.003 ** i) for i in range(300)]
        rows = _ohlcv(closes)
        block = bs.build_technicals(rows)
        self.assertAlmostEqual(block["adx14"], indicators.adx(rows, 14), places=9)

    def test_upvol_and_ad_slope_match_indicators(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        closes = [100.0 + 0.05 * i for i in range(120)]
        rows = _ohlcv(closes)
        block = bs.build_technicals(rows)
        self.assertAlmostEqual(block["upvol_ratio"],
                               indicators.updown_volume(rows, 50), places=9)
        self.assertAlmostEqual(block["ad_line_slope"],
                               indicators.ad_line_slope(rows, 20), places=9)

    def test_stage_2_advancing(self):
        from scripts import build_snapshot as bs
        # last > ma50 > ma200 with both slopes rising: a clean uptrend.
        rows = _ohlcv([100.0 * (1.004 ** i) for i in range(300)])
        self.assertEqual(bs.build_technicals(rows)["stage"], 2)

    def test_stage_4_declining(self):
        from scripts import build_snapshot as bs
        # last < ma50 < ma200 with both slopes falling: a clean downtrend.
        rows = _ohlcv([300.0 * (0.996 ** i) for i in range(300)])
        self.assertEqual(bs.build_technicals(rows)["stage"], 4)

    def test_stage_3_topping(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        # Long rise (so ma200 is still rising) then a recent roll-over so price
        # drops below ma50: stage 3 (topping).
        closes = [100.0 * (1.004 ** i) for i in range(280)]
        top = closes[-1]
        closes += [top * (0.985 ** j) for j in range(1, 21)]  # 20-bar rollover
        rows = _ohlcv(closes)
        block = bs.build_technicals(rows)
        # Preconditions for stage 3: price below ma50, ma200 slope still rising.
        self.assertLess(closes[-1], block["ma50"])
        self.assertGreater(block["ma200_slope_20d"], 0)
        self.assertEqual(block["stage"], 3)

    def test_stage_1_basing_default(self):
        from scripts import build_snapshot as bs
        # A flat series: no MA stack ordering + flat slopes -> stage 1 default.
        rows = _ohlcv([100.0] * 300)
        self.assertEqual(bs.build_technicals(rows)["stage"], 1)

    def test_vwap_52wk_high_anchored_to_argmax(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        # Plant the max adjusted_close 30 bars from the end; the VWAP must anchor
        # to THAT date and equal the indicator over rows on/after it.
        closes = [100.0 + 0.01 * i for i in range(300)]
        closes[270] = 500.0  # unambiguous max, inside trailing 252
        rows = _ohlcv(closes)
        block = bs.build_technicals(rows)
        anchor = rows[270]["date"]
        self.assertAlmostEqual(block["vwap_52wk_high"],
                               indicators.anchored_vwap(rows, anchor), places=6)

    def test_vwap_earnings_uses_next_earnings_date(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        closes = [100.0 + 0.05 * i for i in range(300)]
        rows = _ohlcv(closes)
        anchor = rows[250]["date"]  # a date that exists in the series
        block = bs.build_technicals(rows, next_earnings_date=anchor)
        self.assertAlmostEqual(block["vwap_earnings"],
                               indicators.anchored_vwap(rows, anchor), places=6)

    def test_vwap_earnings_falls_back_to_reported_quarter(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        closes = [100.0 + 0.05 * i for i in range(300)]
        rows = _ohlcv(closes)
        anchor = rows[240]["date"]
        earn_q = [{"reportedDate": anchor, "reportedEPS": "1.0"}]
        # No next_earnings_date -> fall back to the latest reported quarter date.
        block = bs.build_technicals(rows, next_earnings_date=None, earn_q=earn_q)
        self.assertAlmostEqual(block["vwap_earnings"],
                               indicators.anchored_vwap(rows, anchor), places=6)

    def test_vwap_earnings_null_on_future_anchor(self):
        from scripts import build_snapshot as bs
        # A future earnings date (after every row) -> honest None, no crash.
        rows = _ohlcv([100.0 + 0.05 * i for i in range(300)])
        block = bs.build_technicals(rows, next_earnings_date="2099-01-01")
        self.assertIsNone(block["vwap_earnings"])

    def test_null_safety_short_series(self):
        from scripts import build_snapshot as bs
        # A very short series: fields that need more data are None, no exception.
        rows = _ohlcv([100.0, 101.0, 102.0])
        block = bs.build_technicals(rows)
        self.assertIsNone(block["adx14"])        # < 2n+1 rows
        self.assertIsNone(block["ad_line_slope"])  # < lookback+1
        self.assertIsNone(block["upvol_ratio"])    # < n+1
        self.assertIsNone(block["stage"])          # no ma50/ma200
        # vwap_52wk_high still computes over the tiny window (has volume).
        self.assertIsNotNone(block["vwap_52wk_high"])

    def test_full_bundle_carries_wave4a_fields(self):
        # End-to-end: the standard build_full bundle surfaces the new fields.
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        BundleBuilder(d).build_full()
        proc = _run_build(d)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        snap = json.load(open(os.path.join(d, f"snapshot_MU_{AS_OF_DATE}.json")))
        t = snap["technicals"]
        for key in ("adx14", "stage", "ad_line_slope", "upvol_ratio",
                    "vwap_52wk_high", "vwap_earnings"):
            self.assertIn(key, t)
        self.assertIsNotNone(t["adx14"])
        self.assertIn(t["stage"], (1, 2, 3, 4))
        # The fixture's next earnings (2026-09-25) post-dates every row
        # (last 2026-07-15), so vwap_earnings honestly falls back... but the
        # earnings-calendar anchor is future AND earn_q latest reportedDate
        # (2026-07-01) is IN-range -> fallback would apply only if next date is
        # absent. Here next_earnings_date is present+future -> None is correct.
        self.assertIsNone(t["vwap_earnings"])
        self.assertIsNotNone(t["vwap_52wk_high"])


# --------------------------------------------------------------------------- #
# Track O4 — sector-relative RS: SECTOR_ETF map + build_benchmark sector returns
# --------------------------------------------------------------------------- #

class TestSectorEtfMap(unittest.TestCase):
    """O4.1: overview.Sector (GICS-aligned) -> SPDR Select Sector ETF, or None.

    The map is VERIFIED (AV COMPANY_OVERVIEW Sector is GICS-aligned; live bundles
    returned COMMUNICATION SERVICES / TECHNOLOGY / INDUSTRIALS). Unknown/None ->
    None (disclosed absence; NEVER guess an ETF)."""

    def test_sector_etf_maps_verified_gics_sectors(self):
        from scripts import build_snapshot as bs
        self.assertEqual(bs.resolve_sector_etf("COMMUNICATION SERVICES"), "XLC")
        self.assertEqual(bs.resolve_sector_etf("TECHNOLOGY"), "XLK")
        self.assertEqual(bs.resolve_sector_etf("INDUSTRIALS"), "XLI")
        self.assertEqual(bs.resolve_sector_etf("technology"), "XLK")

    def test_sector_etf_omits_unknown_sector(self):
        from scripts import build_snapshot as bs
        self.assertIsNone(bs.resolve_sector_etf("SOME UNLISTED SECTOR"))
        self.assertIsNone(bs.resolve_sector_etf(None))

    def test_sector_etf_alias_rows(self):
        # The verified alias rows (GICS renames / common vendor variants). Only
        # these aliases exist -- no invented ones.
        from scripts import build_snapshot as bs
        self.assertEqual(bs.resolve_sector_etf("INFORMATION TECHNOLOGY"), "XLK")
        self.assertEqual(bs.resolve_sector_etf("CONSUMER CYCLICAL"), "XLY")
        self.assertEqual(bs.resolve_sector_etf("CONSUMER DEFENSIVE"), "XLP")
        self.assertEqual(bs.resolve_sector_etf("FINANCIAL"), "XLF")
        self.assertEqual(bs.resolve_sector_etf("HEALTHCARE"), "XLV")
        self.assertEqual(bs.resolve_sector_etf("BASIC MATERIALS"), "XLB")

    def test_sector_etf_strips_whitespace(self):
        from scripts import build_snapshot as bs
        self.assertEqual(bs.resolve_sector_etf("  Technology  "), "XLK")


class TestBuildBenchmarkSector(unittest.TestCase):
    """O4.2: build_benchmark emits sector returns when sector_rows given, and is
    byte-identical to today when they are absent (graceful disclosed absence)."""

    def _rows(self, closes):
        return [{"adjusted_close": float(c)} for c in closes]

    def test_build_benchmark_adds_sector_returns(self):
        from scripts import build_snapshot as bs
        from scripts import indicators
        # 300 bars so the 12m (252) window is populated for stock + sector.
        stock = self._rows([90.0 + 0.10 * i for i in range(300)])
        spy = self._rows([400.0 + 0.20 * i for i in range(300)])
        sector = self._rows([50.0 + 0.05 * i for i in range(300)])
        res = bs.build_benchmark(stock, spy, sector_rows=sector, sector_etf="XLK")

        self.assertEqual(res["sector_etf"], "XLK")
        sec_adj = [r["adjusted_close"] for r in sector]
        stk_adj = [r["adjusted_close"] for r in stock]
        for w, key in ((bs._W1M, "sector_ret_1m"), (bs._W3M, "sector_ret_3m"),
                       (bs._W6M, "sector_ret_6m"), (bs._W12M, "sector_ret_12m")):
            self.assertAlmostEqual(res[key], indicators.pct_return(sec_adj, w),
                                   places=12)
        # rel_sector_ret_Nm = stock_ret_Nm - sector_ret_Nm (3m and 6m).
        stock_ret_3m = indicators.pct_return(stk_adj, bs._W3M)
        sector_ret_3m = indicators.pct_return(sec_adj, bs._W3M)
        self.assertAlmostEqual(res["rel_sector_ret_3m"],
                               stock_ret_3m - sector_ret_3m, places=12)
        stock_ret_6m = indicators.pct_return(stk_adj, bs._W6M)
        sector_ret_6m = indicators.pct_return(sec_adj, bs._W6M)
        self.assertAlmostEqual(res["rel_sector_ret_6m"],
                               stock_ret_6m - sector_ret_6m, places=12)

    def test_build_benchmark_unchanged_without_sector_rows(self):
        from scripts import build_snapshot as bs
        stock = self._rows([90.0 + 0.10 * i for i in range(300)])
        spy = self._rows([400.0 + 0.20 * i for i in range(300)])
        res = bs.build_benchmark(stock, spy)
        # NONE of the sector keys appear -> benchmark block byte-identical to today.
        for key in ("sector_etf", "sector_ret_1m", "sector_ret_3m",
                    "sector_ret_6m", "sector_ret_12m",
                    "rel_sector_ret_3m", "rel_sector_ret_6m"):
            self.assertNotIn(key, res)

    def test_build_benchmark_sector_none_etf_still_omits(self):
        # sector_rows None (even with an etf label) -> no sector keys emitted.
        from scripts import build_snapshot as bs
        stock = self._rows([90.0 + 0.10 * i for i in range(300)])
        spy = self._rows([400.0 + 0.20 * i for i in range(300)])
        res = bs.build_benchmark(stock, spy, sector_rows=None, sector_etf="XLK")
        self.assertNotIn("sector_etf", res)
        self.assertNotIn("sector_ret_3m", res)


class TestSectorDailyOptionalSource(unittest.TestCase):
    """O4.2 wiring: sector_daily_adjusted is an OPTIONAL source (in COVERS, NOT in
    REQUIRED). A bundle carrying it surfaces sector returns in benchmark; a bundle
    without it builds fine (missing sector data must NEVER fail a snapshot)."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_sector_daily_not_required(self):
        from scripts import build_snapshot as bs
        self.assertNotIn("sector_daily_adjusted", bs.REQUIRED)

    def test_sector_daily_in_covers_as_benchmark(self):
        from scripts import build_snapshot as bs
        self.assertIn("sector_daily_adjusted", bs.COVERS)
        self.assertEqual(bs.COVERS["sector_daily_adjusted"], ["benchmark"])

    def test_bundle_without_sector_builds_and_omits_sector_returns(self):
        # The standard build_full bundle has NO sector_daily_adjusted -> the
        # benchmark block has no sector keys, build still succeeds.
        BundleBuilder(self.dir).build_full()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir,
                               f"snapshot_MU_{AS_OF_DATE}.json")) as fh:
            snap = json.load(fh)
        bm = snap["benchmark"]
        self.assertNotIn("sector_ret_3m", bm)
        self.assertNotIn("sector_etf", bm)

    def test_bundle_with_sector_surfaces_sector_returns(self):
        # Add a sector_daily_adjusted file (XLK for the TECHNOLOGY overview) and
        # wire the overview Sector so resolve_sector_etf finds XLK.
        b = BundleBuilder(self.dir)
        b.add_global_quote()
        # overview with Sector = TECHNOLOGY -> resolve_sector_etf -> XLK.
        ov = {
            "Symbol": b.ticker, "Sector": "TECHNOLOGY",
            "MarketCapitalization": f"{b.mktcap:.0f}",
            "SharesOutstanding": f"{b.shares:.0f}", "EPS": "6.00",
            "PERatio": f"{b.last / 6.0:.4f}", "52WeekHigh": "140.00",
            "52WeekLow": "60.00", "Beta": "1.30",
        }
        b._add("overview", "overview.json", ov, "COMPANY_OVERVIEW")
        b.add_daily(); b.add_spy()
        b.add_income(); b.add_balance(); b.add_cashflow(); b.add_earnings()
        # A sector series (reuse a distinct deterministic walk).
        sector_rows = _walk(320, seed=303, start=180.0)
        b._add("sector_daily_adjusted", "sector_daily.json",
               _daily_json(sector_rows), "TIME_SERIES_DAILY_ADJUSTED")
        b.write_manifest()
        proc = _run_build(self.dir)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(os.path.join(self.dir,
                               f"snapshot_MU_{AS_OF_DATE}.json")) as fh:
            snap = json.load(fh)
        bm = snap["benchmark"]
        self.assertEqual(bm["sector_etf"], "XLK")
        self.assertIsNotNone(bm["sector_ret_3m"])
        self.assertIsNotNone(bm["rel_sector_ret_3m"])
        self.assertIsNotNone(bm["rel_sector_ret_6m"])


class TestSecurityMaster(unittest.TestCase):
    """O15: build_security_master pure-function table + snapshot integration."""

    def test_single_class_reconciled_agree(self):
        from scripts import build_snapshot as bs
        price = {
            "last": 50.0,
            "shares_diluted_m": 2000.0,
            "mktcap": 100_000_000_000.0,
            "mktcap_basis": "reconciled_agree",
        }
        overview = {"Name": "Acme Corp"}
        sm = bs.build_security_master(price, overview, "ACME")
        self.assertEqual(sm["ticker"], "ACME")
        self.assertIsNone(sm["share_class"])
        self.assertEqual(sm["class_shares_m"], 2000.0)
        # single-class: issuer_total == class_shares_m
        self.assertEqual(sm["issuer_total_shares_m"], 2000.0)
        self.assertEqual(sm["issuer_diluted_shares_m"], 2000.0)
        self.assertEqual(sm["shares_source"], "av_class_shares")
        self.assertIs(sm["reconciled_to_filing"], True)  # overview present
        self.assertEqual(sm["issuer_mktcap"], 100_000_000_000.0)
        self.assertEqual(sm["other_listed_classes"], [])

    def test_single_class_computed_only(self):
        from scripts import build_snapshot as bs
        # computed_only basis is single-class; issuer_total == class_shares_m.
        price = {
            "last": 50.0,
            "shares_diluted_m": 2000.0,
            "mktcap": 100_000_000_000.0,
            "mktcap_basis": "computed_only",
        }
        sm = bs.build_security_master(price, {"Name": "Acme Corp"}, "ACME")
        self.assertEqual(sm["issuer_total_shares_m"], 2000.0)
        self.assertEqual(sm["shares_source"], "av_class_shares")
        self.assertIs(sm["reconciled_to_filing"], True)

    def test_multi_class_goog_derived(self):
        from scripts import build_snapshot as bs
        price = {
            "last": 351.37,
            "shares_diluted_m": 5499.638,
            "mktcap": 4287617827000.0,
            "mktcap_basis": "overview_authoritative",
        }
        overview = {"Name": "Alphabet Inc Class C"}
        sm = bs.build_security_master(price, overview, "GOOG")
        self.assertEqual(sm["ticker"], "GOOG")
        self.assertEqual(sm["share_class"], "C")
        self.assertEqual(sm["class_shares_m"], 5499.638)
        # derived issuer total = mktcap / last / 1e6 ~= 12202.57 (spec: ~12202)
        self.assertAlmostEqual(sm["issuer_total_shares_m"], 12202.572294, places=4)
        self.assertEqual(int(sm["issuer_total_shares_m"]), 12202)  # ~12202 per spec
        self.assertEqual(sm["issuer_diluted_shares_m"], sm["issuer_total_shares_m"])
        self.assertEqual(sm["shares_source"], "derived: issuer mktcap / class price")
        self.assertIs(sm["reconciled_to_filing"], False)
        self.assertEqual(sm["issuer_mktcap"], 4287617827000.0)
        self.assertEqual(sm["mktcap_basis"], "overview_authoritative")
        self.assertEqual(sm["other_listed_classes"], ["GOOGL"])
        # round-trip exact by construction against the cap.
        self.assertAlmostEqual(
            sm["issuer_total_shares_m"] * 1e6 * price["last"],
            sm["issuer_mktcap"], places=0)

    def test_googl_sibling_is_goog(self):
        from scripts import build_snapshot as bs
        price = {"last": 349.0, "shares_diluted_m": 6000.0,
                 "mktcap": 4.2e12, "mktcap_basis": "overview_authoritative"}
        sm = bs.build_security_master(price, {"Name": "Alphabet Inc Class A"}, "GOOGL")
        self.assertEqual(sm["share_class"], "A")
        self.assertEqual(sm["other_listed_classes"], ["GOOG"])

    def test_unknown_ticker_no_sibling(self):
        from scripts import build_snapshot as bs
        price = {"last": 50.0, "shares_diluted_m": 2000.0,
                 "mktcap": 100e9, "mktcap_basis": "reconciled_agree"}
        sm = bs.build_security_master(price, {"Name": "Acme Corp"}, "ACME")
        self.assertEqual(sm["other_listed_classes"], [])

    def test_degraded_no_overview(self):
        from scripts import build_snapshot as bs
        # No overview + no derivable cap/last -> nulls + "unavailable".
        price = {"last": None, "shares_diluted_m": None,
                 "mktcap": None, "mktcap_basis": None}
        sm = bs.build_security_master(price, None, "ACME")
        self.assertIsNone(sm["share_class"])
        self.assertIsNone(sm["issuer_total_shares_m"])
        self.assertIsNone(sm["issuer_diluted_shares_m"])
        self.assertIsNone(sm["issuer_mktcap"])
        self.assertEqual(sm["shares_source"], "unavailable")
        self.assertIs(sm["reconciled_to_filing"], False)
        self.assertEqual(sm["other_listed_classes"], [])

    def test_multi_class_degraded_when_no_last(self):
        from scripts import build_snapshot as bs
        # overview_authoritative but no usable price -> cannot derive -> unavailable.
        price = {"last": None, "shares_diluted_m": 5499.638,
                 "mktcap": 4.28e12, "mktcap_basis": "overview_authoritative"}
        sm = bs.build_security_master(price, {"Name": "Alphabet Inc Class C"}, "GOOG")
        self.assertIsNone(sm["issuer_total_shares_m"])
        self.assertEqual(sm["shares_source"], "unavailable")

    def test_parse_share_class_variants(self):
        from scripts import build_snapshot as bs
        self.assertEqual(bs._parse_share_class("Alphabet Inc Class C"), "C")
        self.assertEqual(bs._parse_share_class("Berkshire Hathaway Class B"), "B")
        self.assertIsNone(bs._parse_share_class("Apple Inc"))
        self.assertIsNone(bs._parse_share_class(None))
        self.assertIsNone(bs._parse_share_class(""))

    def test_snapshot_has_security_master_sibling_of_price(self):
        from scripts import build_snapshot as bs
        d = tempfile.mkdtemp()
        try:
            BundleBuilder(d).build_full()
            snap, _ = bs.build_snapshot(d, "MU")
            self.assertIn("security_master", snap)
            sm = snap["security_master"]
            self.assertEqual(sm["ticker"], "MU")
            self.assertEqual(sm["issuer_mktcap"], snap["price"]["mktcap"])
            self.assertIn("shares_source", sm)
            self.assertIn("reconciled_to_filing", sm)
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_security_master_is_scorer_invariant(self):
        """BYTE-IDENTICAL GUARD: the security_master block is additive/ignored.

        Scoring fundamental + risk on a snapshot WITH vs WITHOUT the block must
        produce byte-identical module output (the scorers never read it).
        """
        from scripts import build_snapshot as bs
        from scripts import score_fundamental as sf
        from scripts import score_risk as sr
        d = tempfile.mkdtemp()
        try:
            BundleBuilder(d).build_full()
            snap, _ = bs.build_snapshot(d, "MU")
            self.assertIn("security_master", snap)

            snap_without = {k: v for k, v in snap.items() if k != "security_master"}

            fund_with = sf.build_module(snap)
            fund_without = sf.build_module(snap_without)
            self.assertEqual(
                json.dumps(fund_with, sort_keys=True),
                json.dumps(fund_without, sort_keys=True),
                "score_fundamental changed when security_master added",
            )

            ladder = [
                {"level": 96.0, "type": "ma50", "basis": "test"},
                {"level": 110.0, "type": "swing_high", "basis": "test"},
            ]
            risk_with = sr.build_module(snap, ladder, 0.20, "test risk")
            risk_without = sr.build_module(snap_without, ladder, 0.20, "test risk")
            self.assertEqual(
                json.dumps(risk_with, sort_keys=True),
                json.dumps(risk_without, sort_keys=True),
                "score_risk changed when security_master added",
            )
        finally:
            shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- #
# QC1: NTM EPS time-weighted fiscal-year blend + corrected next_fy_consensus +
# cross-vendor forward P/E ingestion. Ground truth is the REAL AAPL
# (as_of 2026-08-07) / MU (as_of 2026-08-08) earnings_estimates.json payloads
# (see docs/QC_REMEDIATION_TRACKER.md QC1) -- both carry only 2 future fiscal
# QUARTERS (the len(fq)>=4 sum path never fires) but 2 future fiscal YEARS,
# which is exactly the case the pre-QC1 code silently degraded to the
# nearly-expired "nearest_future_fiscal_year" (really the CURRENT FY).
# --------------------------------------------------------------------------- #

class TestQC1TimeWeightedNtmEpsBlend(unittest.TestCase):
    _EMPTY_Q = {"quarterlyReports": []}
    _EMPTY_EARN = {"quarterlyEarnings": []}

    # Verbatim (numeric fields as AV strings) future FQ/FY rows from the real
    # bundles' earnings_estimates.json.
    _AAPL_ESTIMATES = [
        {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.9755"},
        {"date": "2026-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "2.9091"},
        {"date": "2026-09-30", "horizon": "fiscal year", "eps_estimate_average": "8.7998",
         "eps_estimate_average_90_days_ago": "8.7658",
         "eps_estimate_revision_up_trailing_30_days": "5",
         "eps_estimate_revision_down_trailing_30_days": "2",
         "revenue_estimate_average": "477372745170.00"},
        {"date": "2027-09-30", "horizon": "fiscal year", "eps_estimate_average": "9.5490",
         "eps_estimate_average_90_days_ago": "9.6186",
         "eps_estimate_revision_up_trailing_30_days": "7",
         "eps_estimate_revision_down_trailing_30_days": "1",
         "revenue_estimate_average": "523333033660.00"},
    ]
    _MU_ESTIMATES = [
        {"date": "2026-08-31", "horizon": "fiscal quarter", "eps_estimate_average": "31.3290"},
        {"date": "2026-11-30", "horizon": "fiscal quarter", "eps_estimate_average": "34.8662"},
        {"date": "2026-08-31", "horizon": "fiscal year", "eps_estimate_average": "73.4440",
         "eps_estimate_average_90_days_ago": "57.8371",
         "eps_estimate_revision_up_trailing_30_days": "29",
         "eps_estimate_revision_down_trailing_30_days": "0",
         "revenue_estimate_average": "129779271370.00"},
        {"date": "2027-08-31", "horizon": "fiscal year", "eps_estimate_average": "153.7395",
         "eps_estimate_average_90_days_ago": "100.5257",
         "eps_estimate_revision_up_trailing_30_days": "30",
         "eps_estimate_revision_down_trailing_30_days": "0",
         "revenue_estimate_average": "238816685730.00"},
    ]

    def _fundamentals(self, estimates, as_of_date, eps_overview="8.75"):
        from scripts import build_snapshot as bs
        return bs.build_fundamentals(
            self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_EARN,
            estimates, {"EPS": eps_overview}, as_of_date)

    def test_aapl_blend_matches_measured_ground_truth(self):
        f = self._fundamentals(self._AAPL_ESTIMATES, "2026-08-07")
        self.assertEqual(f["eps_ntm_method"], "time_weighted_fiscal_years")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 9.411997808219176, places=6)
        self.assertAlmostEqual(f["eps_ntm_coverage"], 0.9972602739726026, places=6)
        self.assertIsInstance(f["eps_ntm_basis"], str)

    def test_mu_blend_matches_measured_ground_truth(self):
        f = self._fundamentals(self._MU_ESTIMATES, "2026-08-08")
        self.assertEqual(f["eps_ntm_method"], "time_weighted_fiscal_years")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 148.2585794520548, places=5)
        self.assertAlmostEqual(f["eps_ntm_coverage"], 0.9972602739726028, places=6)

    def test_aapl_blend_alternatives_disclosure_shape(self):
        # QC1-REGRESSION: the fiscal-year blend is the ONLY path exercised on
        # 22/22 surveyed tickers (AV rarely carries >=4 future fiscal
        # quarters), so its alternatives must ALWAYS be visible -- blended
        # value+method, the value the single-nearest-future-FY proxy WOULD
        # have produced (the pre-QC1 method), the vendor ForwardPE-implied
        # EPS, and the per-FY-record weights actually used -- so a reader can
        # reconstruct and judge the alternative without re-fetching anything.
        from scripts import build_snapshot as bs
        price = {"last": 312.41}
        overview = {"EPS": "8.75", "ForwardPE": "31.95"}
        f = bs.build_fundamentals(
            self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_EARN,
            self._AAPL_ESTIMATES, overview, "2026-08-07", price=price)
        alt = f["eps_ntm_alternatives"]
        self.assertIsNotNone(alt)
        self.assertAlmostEqual(alt["blended_value"], 9.411997808219176, places=6)
        self.assertEqual(alt["blended_method"], "time_weighted_fiscal_years")
        # single-FY alternative: the nearest future FY row alone (8.7998),
        # exactly what the pre-QC1 degraded method would have used.
        self.assertAlmostEqual(alt["single_fy_proxy_value"], 8.7998, places=4)
        self.assertEqual(alt["single_fy_proxy_fiscal_date_ending"], "2026-09-30")
        self.assertAlmostEqual(alt["vendor_forward_pe"], 31.95)
        self.assertAlmostEqual(alt["vendor_forward_pe_implied_eps"],
                               312.41 / 31.95, places=6)
        self.assertEqual(len(alt["fy_weights"]), 2)
        w0, w1 = alt["fy_weights"]
        self.assertEqual(w0["fiscal_date_ending"], "2026-09-30")
        self.assertAlmostEqual(w0["eps_estimate_average"], 8.7998, places=4)
        self.assertAlmostEqual(w0["weight"], 0.147945, places=5)
        self.assertEqual(w1["fiscal_date_ending"], "2027-09-30")
        self.assertAlmostEqual(w1["eps_estimate_average"], 9.5490, places=4)
        self.assertAlmostEqual(w1["weight"], 0.849315, places=5)

    def test_mu_blend_alternatives_disclosure_shape(self):
        from scripts import build_snapshot as bs
        price = {"last": 877.57}
        overview = {"EPS": "8.75", "ForwardPE": "5.31"}
        f = bs.build_fundamentals(
            self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_Q, self._EMPTY_EARN,
            self._MU_ESTIMATES, overview, "2026-08-08", price=price)
        alt = f["eps_ntm_alternatives"]
        self.assertIsNotNone(alt)
        self.assertAlmostEqual(alt["blended_value"], 148.2585794520548, places=5)
        self.assertAlmostEqual(alt["single_fy_proxy_value"], 73.444, places=3)
        self.assertEqual(alt["single_fy_proxy_fiscal_date_ending"], "2026-08-31")
        self.assertAlmostEqual(alt["vendor_forward_pe"], 5.31)
        self.assertAlmostEqual(alt["vendor_forward_pe_implied_eps"],
                               877.57 / 5.31, places=5)
        self.assertEqual(len(alt["fy_weights"]), 2)
        w0, w1 = alt["fy_weights"]
        self.assertEqual(w0["fiscal_date_ending"], "2026-08-31")
        self.assertAlmostEqual(w0["weight"], 0.063014, places=5)
        self.assertEqual(w1["fiscal_date_ending"], "2027-08-31")
        self.assertAlmostEqual(w1["weight"], 0.934247, places=5)

    def test_alternatives_degrade_to_none_fields_without_price_or_vendor_pe(self):
        # "when available" -- price/vendor ForwardPE absent must NOT crash;
        # the blend + single-FY alternative + weights are still disclosed
        # (they don't depend on price or the vendor at all).
        f = self._fundamentals(self._AAPL_ESTIMATES, "2026-08-07")  # no price kwarg
        alt = f["eps_ntm_alternatives"]
        self.assertIsNotNone(alt)
        self.assertAlmostEqual(alt["blended_value"], 9.411997808219176, places=6)
        self.assertAlmostEqual(alt["single_fy_proxy_value"], 8.7998, places=4)
        self.assertIsNone(alt["vendor_forward_pe"])
        self.assertIsNone(alt["vendor_forward_pe_implied_eps"])
        self.assertEqual(len(alt["fy_weights"]), 2)

    def test_alternatives_absent_when_blend_path_not_used(self):
        # The sum-of-4-future-quarters path is the FULL (undegraded) method --
        # no blend ran, so there is nothing to disclose an alternative for.
        estimates = [
            {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.00"},
            {"date": "2026-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "1.10"},
            {"date": "2027-03-31", "horizon": "fiscal quarter", "eps_estimate_average": "1.20"},
            {"date": "2027-06-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.30"},
            {"date": "2028-06-30", "horizon": "fiscal year", "eps_estimate_average": "5.00"},
        ]
        f = self._fundamentals(estimates, "2026-08-07")
        self.assertEqual(f["eps_ntm_method"], "sum_next_4_fiscal_quarters")
        self.assertIsNone(f["eps_ntm_alternatives"])

    def test_aapl_next_fy_consensus_corrected(self):
        # FY2026-09-30's span contains as_of (2026-08-07) -> CURRENT fy.
        # FY2027-09-30 is the correct "next" -> eps 9.5490 (not 8.7998).
        f = self._fundamentals(self._AAPL_ESTIMATES, "2026-08-07")
        self.assertAlmostEqual(f["next_fy_consensus"]["eps"], 9.5490, places=4)
        self.assertAlmostEqual(f["next_fy_consensus"]["rev"], 523333033660.00, places=2)
        self.assertEqual(f["next_fy_basis"]["current_fy_end"], "2026-09-30")
        self.assertEqual(f["next_fy_basis"]["next_fy_end"], "2027-09-30")

    def test_mu_next_fy_consensus_corrected(self):
        f = self._fundamentals(self._MU_ESTIMATES, "2026-08-08")
        self.assertAlmostEqual(f["next_fy_consensus"]["eps"], 153.7395, places=4)
        self.assertEqual(f["next_fy_basis"]["current_fy_end"], "2026-08-31")
        self.assertEqual(f["next_fy_basis"]["next_fy_end"], "2027-08-31")

    def test_sum_next_4_fiscal_quarters_path_untouched(self):
        # >=4 future FQ rows must still take the original sum path unchanged.
        estimates = [
            {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.00"},
            {"date": "2026-12-31", "horizon": "fiscal quarter", "eps_estimate_average": "1.10"},
            {"date": "2027-03-31", "horizon": "fiscal quarter", "eps_estimate_average": "1.20"},
            {"date": "2027-06-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.30"},
            {"date": "2028-06-30", "horizon": "fiscal year", "eps_estimate_average": "5.00"},
        ]
        f = self._fundamentals(estimates, "2026-08-07")
        self.assertEqual(f["eps_ntm_method"], "sum_next_4_fiscal_quarters")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 1.00 + 1.10 + 1.20 + 1.30)
        self.assertIsNone(f["eps_ntm_coverage"])

    def test_single_future_fy_is_degraded_not_mislabeled_nearest(self):
        estimates = [
            {"date": "2026-09-30", "horizon": "fiscal quarter", "eps_estimate_average": "1.00"},
            {"date": "2027-06-30", "horizon": "fiscal year", "eps_estimate_average": "5.00"},
        ]
        f = self._fundamentals(estimates, "2026-08-07")
        self.assertEqual(f["eps_ntm_method"], "single_future_fiscal_year_degraded_proxy")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 5.00)
        self.assertIn("DEGRADED", f["eps_ntm_basis"])

    def test_valuation_pe_fwd_and_pe_overview_fwd_aapl(self):
        from scripts import build_snapshot as bs
        f = self._fundamentals(self._AAPL_ESTIMATES, "2026-08-07")
        price = {"last": 312.41, "mktcap": None, "mktcap_computed": None}
        overview = {"ForwardPE": "31.95", "PERatio": "35.70"}
        v = bs.build_valuation(price, f, overview, rows=[])
        self.assertAlmostEqual(v["pe_fwd"], 33.192740411305984, places=5)
        self.assertAlmostEqual(v["pe_overview_fwd"], 31.95)

    def test_valuation_pe_fwd_and_pe_overview_fwd_mu(self):
        from scripts import build_snapshot as bs
        f = self._fundamentals(self._MU_ESTIMATES, "2026-08-08")
        price = {"last": 877.57, "mktcap": None, "mktcap_computed": None}
        overview = {"ForwardPE": "5.31", "PERatio": "19.92"}
        v = bs.build_valuation(price, f, overview, rows=[])
        self.assertAlmostEqual(v["pe_fwd"], 5.919185272403049, places=5)
        self.assertAlmostEqual(v["pe_overview_fwd"], 5.31)

    def test_pe_overview_fwd_absent_when_overview_missing_field(self):
        from scripts import build_snapshot as bs
        f = self._fundamentals(self._AAPL_ESTIMATES, "2026-08-07")
        v = bs.build_valuation({"last": 312.41}, f, {}, rows=[])
        self.assertIsNone(v["pe_overview_fwd"])


# --------------------------------------------------------------------------- #
# QC3: eps_ttm_from_ni (three-way TTM EPS reconciliation input) +
# eps_share_reconciliation (per-quarter implied-share-count, joined on the
# SAME quarter's balance-sheet report). Ground truth is the REAL AAPL
# (as_of 2026-08-07) / MU (as_of 2026-08-08) quarterly income/balance/
# earnings reports (see docs/QC_REMEDIATION_TRACKER.md QC3). AAPL's
# 2026-06-30 reportedEPS (1.91) is a KNOWN vendor data-corruption row.
# --------------------------------------------------------------------------- #

class TestQC3EpsReconciliation(unittest.TestCase):
    _AAPL_INCOME_Q = [
        {"fiscalDateEnding": "2026-06-30", "netIncome": "29789000000"},
        {"fiscalDateEnding": "2026-03-31", "netIncome": "29578000000"},
        {"fiscalDateEnding": "2025-12-31", "netIncome": "42097000000"},
        {"fiscalDateEnding": "2025-09-30", "netIncome": "27466000000"},
    ]
    _AAPL_BALANCE_Q = [
        {"fiscalDateEnding": "2026-06-30", "commonStockSharesOutstanding": "14750302000"},
        {"fiscalDateEnding": "2026-03-31", "commonStockSharesOutstanding": "14768115000"},
        {"fiscalDateEnding": "2025-12-31", "commonStockSharesOutstanding": "14810356000"},
        {"fiscalDateEnding": "2025-09-30", "commonStockSharesOutstanding": "15004697000"},
    ]
    _AAPL_EARNINGS_Q = [
        {"fiscalDateEnding": "2026-06-30", "reportedEPS": "1.91"},
        {"fiscalDateEnding": "2026-03-31", "reportedEPS": "2.01"},
        {"fiscalDateEnding": "2025-12-31", "reportedEPS": "2.84"},
        {"fiscalDateEnding": "2025-09-30", "reportedEPS": "1.85"},
    ]

    _MU_INCOME_Q = [
        {"fiscalDateEnding": "2026-05-31", "netIncome": "28243000000"},
        {"fiscalDateEnding": "2026-02-28", "netIncome": "13789000000"},
        {"fiscalDateEnding": "2025-11-30", "netIncome": "5240000000"},
        {"fiscalDateEnding": "2025-08-31", "netIncome": "3201000000"},
    ]
    _MU_BALANCE_Q = [
        {"fiscalDateEnding": "2026-05-31", "commonStockSharesOutstanding": "1145000000"},
        {"fiscalDateEnding": "2026-02-28", "commonStockSharesOutstanding": "1140000000"},
        {"fiscalDateEnding": "2025-11-30", "commonStockSharesOutstanding": "1138000000"},
        {"fiscalDateEnding": "2025-08-31", "commonStockSharesOutstanding": "1125000000"},
    ]
    _MU_EARNINGS_Q = [
        {"fiscalDateEnding": "2026-05-31", "reportedEPS": "24.89"},
        {"fiscalDateEnding": "2026-02-28", "reportedEPS": "12.20"},
        {"fiscalDateEnding": "2025-11-30", "reportedEPS": "4.78"},
        {"fiscalDateEnding": "2025-08-31", "reportedEPS": "3.03"},
    ]

    def _fundamentals(self, income_q, balance_q, earnings_q, eps_overview, as_of_date):
        from scripts import build_snapshot as bs
        income = {"quarterlyReports": income_q}
        balance = {"quarterlyReports": balance_q}
        cashflow = {"quarterlyReports": []}
        earnings = {"quarterlyEarnings": earnings_q}
        return bs.build_fundamentals(income, balance, cashflow, earnings, [],
                                     {"EPS": eps_overview}, as_of_date)

    def test_aapl_eps_ttm_from_ni_and_shares_basis(self):
        f = self._fundamentals(self._AAPL_INCOME_Q, self._AAPL_BALANCE_Q,
                               self._AAPL_EARNINGS_Q, "8.75", "2026-08-07")
        self.assertAlmostEqual(f["eps_ttm"], 8.75)
        self.assertAlmostEqual(f["eps_ttm_computed"], 8.61)
        self.assertAlmostEqual(f["eps_ttm_from_ni"], 8.74083798419856, places=6)
        self.assertAlmostEqual(f["eps_ttm_from_ni_shares"], 14_750_302_000.0)
        # Disclosure: point-in-time BASIC count, not diluted (AV has no
        # diluted-share field anywhere).
        self.assertIn("BASIC", f["eps_ttm_from_ni_basis"])
        self.assertIn("diluted", f["eps_ttm_from_ni_basis"].lower())

    def test_mu_eps_ttm_from_ni(self):
        f = self._fundamentals(self._MU_INCOME_Q, self._MU_BALANCE_Q,
                               self._MU_EARNINGS_Q, "44.05", "2026-08-08")
        self.assertAlmostEqual(f["eps_ttm"], 44.05)
        self.assertAlmostEqual(f["eps_ttm_computed"], 44.90, places=6)
        self.assertAlmostEqual(f["eps_ttm_from_ni"], 44.08122270742358, places=6)
        self.assertAlmostEqual(f["eps_ttm_from_ni_shares"], 1_145_000_000.0)

    def test_aapl_eps_share_reconciliation_same_quarter_join(self):
        # 4 measured same-quarter divergences pinned exactly; 2026-06-30 is
        # the KNOWN corrupt reportedEPS row (1.91).
        f = self._fundamentals(self._AAPL_INCOME_Q, self._AAPL_BALANCE_Q,
                               self._AAPL_EARNINGS_Q, "8.75", "2026-08-07")
        recon = {r["fiscal_date_ending"]: r for r in f["eps_share_reconciliation"]}
        self.assertEqual(len(recon), 4)
        self.assertAlmostEqual(recon["2026-06-30"]["divergence_pct"],
                               0.057357000455586116, places=6)
        self.assertAlmostEqual(recon["2026-03-31"]["divergence_pct"],
                               -0.0035679647963100473, places=6)
        self.assertAlmostEqual(recon["2025-12-31"]["divergence_pct"],
                               0.0008461190226394722, places=6)
        self.assertAlmostEqual(recon["2025-09-30"]["divergence_pct"],
                               -0.010544065869075102, places=6)

    def test_mu_eps_share_reconciliation_same_quarter_join(self):
        # QC3: the same-quarter join is REQUIRED -- comparing every quarter
        # against the LATEST share count instead would show a monotonically
        # growing (but spurious) divergence on MU's fast-growing share count.
        f = self._fundamentals(self._MU_INCOME_Q, self._MU_BALANCE_Q,
                               self._MU_EARNINGS_Q, "44.05", "2026-08-08")
        recon = {r["fiscal_date_ending"]: r for r in f["eps_share_reconciliation"]}
        self.assertEqual(len(recon), 4)
        self.assertAlmostEqual(recon["2026-05-31"]["divergence_pct"],
                               -0.008984510009982804, places=6)
        self.assertAlmostEqual(recon["2026-02-28"]["divergence_pct"],
                               -0.008556226632154197, places=6)
        self.assertAlmostEqual(recon["2025-11-30"]["divergence_pct"],
                               -0.036700958151642385, places=6)
        self.assertAlmostEqual(recon["2025-08-31"]["divergence_pct"],
                               -0.06094609460946094, places=6)

    def test_reconciliation_skips_quarter_without_same_quarter_balance_row(self):
        # A quarter with no matching same-quarter balance-sheet row must be
        # OMITTED, never guessed by borrowing a different quarter's shares.
        balance_q = self._AAPL_BALANCE_Q[:3]  # drop 2025-09-30
        f = self._fundamentals(self._AAPL_INCOME_Q, balance_q,
                               self._AAPL_EARNINGS_Q, "8.75", "2026-08-07")
        dates = {r["fiscal_date_ending"] for r in f["eps_share_reconciliation"]}
        self.assertNotIn("2025-09-30", dates)
        self.assertEqual(len(f["eps_share_reconciliation"]), 3)


# --------------------------------------------------------------------------- #
# QC5: build_benchmark wiring for the 5y-monthly beta. Exact AAPL/MU ground-
# truth beta values are pinned at the indicator level (test_indicators.py
# TestBetaCorrMonthly, off the REAL AAPL/MU month-end series); this class
# exercises build_benchmark's WIRING (beta_basis/beta_n_obs/beta_n_days/
# beta_vendor, degrade paths) with synthetic multi-year data.
# --------------------------------------------------------------------------- #

class TestQC5MonthlyBetaWiring(unittest.TestCase):
    def _dated_rows(self, n_months, seed, start=100.0):
        """n_months of ascending month-end dated rows, deterministic LCG walk."""
        import calendar
        import datetime as _dt
        state = seed & 0xFFFFFFFF
        price = start
        rows = []
        y, m = 2015, 1
        for _ in range(n_months):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            r = ((state / 0x7FFFFFFF) - 0.5) * 0.05
            price = price * (1 + r)
            last_day = calendar.monthrange(y, m)[1]
            rows.append({"date": _dt.date(y, m, last_day).isoformat(),
                        "adjusted_close": round(price, 6)})
            m += 1
            if m > 12:
                m = 1
                y += 1
        return rows

    def test_beta_fields_wired_from_monthly_indicator(self):
        from scripts import build_snapshot as bs
        stock = self._dated_rows(70, seed=11, start=100.0)
        spy = self._dated_rows(70, seed=22, start=400.0)
        bm = bs.build_benchmark(stock, spy, overview={"Beta": "1.30"})
        self.assertEqual(bm["beta_n_obs"], 60)
        self.assertFalse(bm["beta_basis"]["degraded"])
        self.assertEqual(bm["beta_basis"]["method"], "5y_monthly")
        self.assertEqual(bm["beta_basis"]["benchmark_symbol"], "SPY")
        self.assertEqual(bm["beta_basis"]["frequency"], "monthly")
        self.assertEqual(bm["beta_basis"]["n_obs"], 60)
        self.assertIn("WACC", bm["beta_basis"]["note"])
        self.assertAlmostEqual(bm["beta_vendor"], 1.30)
        # beta_n_days = CALENDAR days spanned by the window, NOT obs count
        # (score_risk._MIN_BETA_N_DAYS=150 reads this field).
        self.assertGreater(bm["beta_n_days"], 1700)
        self.assertNotEqual(bm["beta_n_days"], bm["beta_n_obs"])
        self.assertIsNotNone(bm["beta"])
        self.assertIsNotNone(bm["corr"])

    def test_degraded_short_history_still_reports_a_beta(self):
        from scripts import build_snapshot as bs
        stock = self._dated_rows(30, seed=11, start=100.0)
        spy = self._dated_rows(30, seed=22, start=400.0)
        bm = bs.build_benchmark(stock, spy)
        self.assertIsNotNone(bm["beta"])
        self.assertEqual(bm["beta_n_obs"], 29)
        self.assertTrue(bm["beta_basis"]["degraded"])
        self.assertIn("degraded_reason", bm["beta_basis"])

    def test_insufficient_history_returns_null_beta_with_disclosure(self):
        from scripts import build_snapshot as bs
        stock = self._dated_rows(10, seed=11, start=100.0)
        spy = self._dated_rows(10, seed=22, start=400.0)
        bm = bs.build_benchmark(stock, spy)
        self.assertIsNone(bm["beta"])
        self.assertIsNone(bm["corr"])
        self.assertIsNone(bm["beta_n_obs"])
        self.assertIsNone(bm["beta_n_days"])
        self.assertTrue(bm["beta_basis"]["degraded"])
        self.assertIn("degraded_reason", bm["beta_basis"])

    def test_beta_vendor_absent_when_overview_missing(self):
        from scripts import build_snapshot as bs
        stock = self._dated_rows(70, seed=11)
        spy = self._dated_rows(70, seed=22)
        bm = bs.build_benchmark(stock, spy)
        self.assertIsNone(bm["beta_vendor"])

    def test_rows_without_dates_never_crash(self):
        # Legacy dateless fixtures (e.g. TestBuildBenchmarkSector's rows,
        # which carry only adjusted_close) must still build without error.
        from scripts import build_snapshot as bs
        stock = [{"adjusted_close": 100.0 + i} for i in range(300)]
        spy = [{"adjusted_close": 400.0 + i} for i in range(300)]
        bm = bs.build_benchmark(stock, spy)
        self.assertIsNone(bm["beta"])
        self.assertIsNone(bm["corr"])


# --------------------------------------------------------------------------- #
# QC12: era-correct rolling-TTM P/E median. Pre-fix, pe_5yr_median/
# pe_10yr_median divided EVERY historical bar's adjusted_close by TODAY's
# single eps_ttm ("approx_current_eps") -- a rescaled PRICE median, not an
# earnings-history multiple. The fix: for each bar d, TTM_EPS(d) = sum of the
# 4 most recent quarterlyEarnings.reportedEPS with reportedDate <= d (skip the
# bar if <4 such quarters or TTM_EPS(d) <= 0); P/E(d) = RAW close / TTM_EPS(d)
# -- never adjusted_close, since reportedEPS is never retroactively
# split-adjusted while adjusted_close IS. See docs/QC_REMEDIATION_TRACKER.md
# QC12.
# --------------------------------------------------------------------------- #

class TestQC12RollingTtmPeSeries(unittest.TestCase):
    """Unit tests for build_snapshot.rolling_ttm_pe_series -- the shared
    era-correct construction (reused by render_charts' pe_band chart), pinned
    on small hand-computed fixtures."""

    @staticmethod
    def _rows(dated):
        return [{"date": d, "close": c, "adjusted_close": a} for d, c, a in dated]

    def test_skips_bars_before_4_quarters_reported(self):
        from scripts import build_snapshot as bs
        # Only 3 quarters exist -- every bar must be skipped (never a bogus
        # sub-4-quarter TTM).
        earn_q = [
            {"reportedDate": "2023-01-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-10-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-07-15", "reportedEPS": "1.0"},
        ]
        rows = self._rows([("2023-02-01", 50.0, 50.0), ("2023-03-01", 55.0, 55.0)])
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["points"], [])
        self.assertEqual(out["n_skipped"], 2)
        self.assertEqual(out["n_eps_records"], 3)
        self.assertEqual(out["n_bars"], 2)

    def test_skips_bars_with_non_positive_ttm_eps(self):
        from scripts import build_snapshot as bs
        earn_q = [
            {"reportedDate": "2023-01-15", "reportedEPS": "-5.0"},
            {"reportedDate": "2022-10-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-07-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-04-15", "reportedEPS": "1.0"},
        ]
        # TTM at 2023-02-01 = -5+1+1+1 = -2 <= 0 -> skipped, not reported as
        # a nonsensical negative P/E.
        rows = self._rows([("2023-02-01", 50.0, 50.0)])
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["points"], [])
        self.assertEqual(out["n_skipped"], 1)

    def test_uses_raw_close_not_adjusted_close(self):
        from scripts import build_snapshot as bs
        earn_q = [
            {"reportedDate": "2023-01-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-10-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-07-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-04-15", "reportedEPS": "1.0"},
        ]
        # TTM = 4.0. adjusted_close is HALF of close here (simulating a 2:1
        # split baked into the adjusted series): the era-correct P/E must
        # divide the RAW close, never the split-adjusted one, or it silently
        # breaks across any split (this is the load-bearing QC12 requirement).
        rows = self._rows([("2023-02-01", 100.0, 50.0)])
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(len(out["points"]), 1)
        date, pe = out["points"][0]
        self.assertEqual(date, "2023-02-01")
        self.assertAlmostEqual(pe, 100.0 / 4.0)

    def test_ttm_sums_only_the_4_most_recent_quarters(self):
        from scripts import build_snapshot as bs
        earn_q = [
            {"reportedDate": "2023-01-15", "reportedEPS": "2.0"},
            {"reportedDate": "2022-10-15", "reportedEPS": "2.0"},
            {"reportedDate": "2022-07-15", "reportedEPS": "2.0"},
            {"reportedDate": "2022-04-15", "reportedEPS": "2.0"},
            {"reportedDate": "2022-01-15", "reportedEPS": "999.0"},  # 5th-newest: excluded
        ]
        rows = self._rows([("2023-02-01", 80.0, 80.0)])
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        _, pe = out["points"][0]
        self.assertAlmostEqual(pe, 80.0 / 8.0)  # 2+2+2+2, NOT +999

    def test_a_new_quarter_report_shifts_ttm_mid_series(self):
        from scripts import build_snapshot as bs
        earn_q = [
            {"reportedDate": "2023-02-10", "reportedEPS": "3.0"},  # newest
            {"reportedDate": "2022-11-10", "reportedEPS": "1.0"},
            {"reportedDate": "2022-08-10", "reportedEPS": "1.0"},
            {"reportedDate": "2022-05-10", "reportedEPS": "1.0"},
            {"reportedDate": "2022-02-10", "reportedEPS": "1.0"},
        ]
        rows = self._rows([
            ("2023-02-05", 40.0, 40.0),   # BEFORE the 02-10 report: TTM=4.0
            ("2023-02-15", 40.0, 40.0),   # AFTER it: TTM=1+1+1+3=6.0
        ])
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        pes = dict(out["points"])
        self.assertAlmostEqual(pes["2023-02-05"], 40.0 / 4.0)
        self.assertAlmostEqual(pes["2023-02-15"], 40.0 / 6.0)

    def test_missing_close_or_date_is_skipped_not_crashed(self):
        from scripts import build_snapshot as bs
        earn_q = [
            {"reportedDate": "2023-01-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-10-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-07-15", "reportedEPS": "1.0"},
            {"reportedDate": "2022-04-15", "reportedEPS": "1.0"},
        ]
        rows = [{"date": "2023-02-01", "close": None},
                {"date": None, "close": 50.0}]
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["points"], [])
        self.assertEqual(out["n_skipped"], 2)


class TestQC12PercentileAndMedianHelpers(unittest.TestCase):
    def test_median_odd(self):
        from scripts import build_snapshot as bs
        self.assertAlmostEqual(bs._median_sorted([1.0, 3.0, 5.0]), 3.0)

    def test_median_even(self):
        from scripts import build_snapshot as bs
        self.assertAlmostEqual(bs._median_sorted([1.0, 2.0, 3.0, 4.0]), 2.5)

    def test_median_empty_is_none(self):
        from scripts import build_snapshot as bs
        self.assertIsNone(bs._median_sorted([]))

    def test_percentile_linear_matches_known_values(self):
        from scripts import build_snapshot as bs
        # numpy.percentile([1..10], 25) == 3.25; 75 -> 7.75 (linear/type-7,
        # the method that reproduces the QC12 ground truth exactly).
        vals = [float(x) for x in range(1, 11)]
        self.assertAlmostEqual(bs._percentile_linear(vals, 0.25), 3.25)
        self.assertAlmostEqual(bs._percentile_linear(vals, 0.75), 7.75)

    def test_percentile_linear_empty_is_none(self):
        from scripts import build_snapshot as bs
        self.assertIsNone(bs._percentile_linear([], 0.25))


class TestQC12Evaluability(unittest.TestCase):
    """The PRIMARY input-based evaluability gate (coverage floor / median
    plausibility / EPS-series validity) -- keyed on INPUTS, never the
    pe_fwd/pe_5yr_median ratio the scorers use as a SECONDARY backstop. The
    ratio missed MU because numerator and denominator were BOTH distorted by
    the same EPS regime and cancelled back inside [0.2, 5.0]."""

    def test_all_pass_is_ok(self):
        from scripts import build_snapshot as bs
        ok, reason = bs._pe_evaluability(30.0, 1000, 1260, 40, True)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_guard1_coverage_floor(self):
        from scripts import build_snapshot as bs
        ok, reason = bs._pe_evaluability(30.0, 500, 1260, 40, True)  # 39.7%
        self.assertFalse(ok)
        self.assertIn("coverage_below_floor", reason)

    def test_guard2_median_plausibility_catches_shipped_mu(self):
        from scripts import build_snapshot as bs
        # The SHIPPED (pre-fix) MU 5yr median (1.947830096821801), otherwise
        # healthy inputs: guard 2 ALONE is what would have caught it.
        ok, reason = bs._pe_evaluability(1.947830096821801, 947, 1260, 40, True)
        self.assertFalse(ok)
        self.assertIn("median_outside_plausible_band", reason)

    def test_guard2_does_not_catch_shipped_aapl(self):
        from scripts import build_snapshot as bs
        # The SHIPPED (pre-fix) AAPL 5yr median (21.198563518380485) lands
        # INSIDE [3,150] -- NO input guard would have caught it; only the
        # construction fix corrects AAPL (this is the plain statement QC12's
        # remediation report requires).
        ok, reason = bs._pe_evaluability(21.198563518380485, 1260, 1260, 40, True)
        self.assertTrue(ok)
        self.assertEqual(reason, "ok")

    def test_guard3_too_few_eps_records(self):
        from scripts import build_snapshot as bs
        ok, reason = bs._pe_evaluability(30.0, 1000, 1260, 3, True)
        self.assertFalse(ok)
        self.assertIn("insufficient_eps_series", reason)

    def test_guard3_none_usable_at_window_start(self):
        from scripts import build_snapshot as bs
        ok, reason = bs._pe_evaluability(30.0, 1000, 1260, 40, False)
        self.assertFalse(ok)
        self.assertIn("insufficient_eps_series", reason)

    def test_corrected_aapl_and_mu_medians_pass(self):
        from scripts import build_snapshot as bs
        # Corrected AAPL 30.7333 / MU 17.8893 both pass every guard.
        ok_a, reason_a = bs._pe_evaluability(30.7333, 1260, 1260, 122, True)
        ok_m, reason_m = bs._pe_evaluability(17.8893, 947, 1260, 122, True)
        self.assertTrue(ok_a)
        self.assertEqual(reason_a, "ok")
        self.assertTrue(ok_m)
        self.assertEqual(reason_m, "ok")


class TestQC12BuildValuationWiring(unittest.TestCase):
    def test_pe_median_method_is_rolling_ttm(self):
        from scripts import build_snapshot as bs
        v = bs.build_valuation({"last": 100.0}, {"eps_ntm_consensus": 5.0}, {}, rows=[])
        self.assertEqual(v["pe_median_method"], "rolling_ttm_reported_eps")
        self.assertIsNone(v["pe_5yr_median"])  # no rows -> no series
        self.assertIsNone(v["pe_10yr_median"])
        self.assertFalse(v["pe_5yr_evaluable"])

    def test_pe_median_basis_shape_and_evaluability_on_full_coverage_fixture(self):
        from scripts import build_snapshot as bs
        import datetime as _dt
        # 4 quarters, all reported long before the price history starts, so
        # TTM_EPS is a constant 4.0 across the whole window (100% coverage).
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                 for d in ("1990-01-01", "1990-04-01", "1990-07-01", "1990-10-01")]
        day = _dt.date(2000, 1, 1)
        rows = []
        for i in range(1300):
            rows.append({"date": day.isoformat(), "close": 40.0 + (i % 5),
                        "adjusted_close": 20.0 + (i % 5)})
            day += _dt.timedelta(days=1)
        v = bs.build_valuation({"last": 100.0}, {}, {}, rows=rows, earn_q=earn_q)
        self.assertEqual(v["pe_median_method"], "rolling_ttm_reported_eps")
        basis = v["pe_median_basis"]
        self.assertEqual(basis["method"], "rolling_ttm_reported_eps")
        # D2/D3: price_basis now truthfully says "split-adjusted", never the
        # old bare "raw close" (the pre-fix label, paired with a note that
        # asserted a premise later falsified by AAPL's real reportedEPS
        # history -- see TestQC12PriceBasisNoteCorrected).
        self.assertEqual(basis["price_basis"], "split-adjusted close (never dividend-adjusted)")
        self.assertIn("split", basis["price_basis_note"].lower())
        w5 = basis["windows"]["5yr"]
        self.assertEqual(w5["window_bars"], 1260)
        self.assertEqual(w5["n_points"], 1260)
        self.assertEqual(w5["n_skipped"], 0)
        self.assertAlmostEqual(w5["coverage"], 1.0)
        self.assertIsNotNone(w5["median"])
        self.assertIn("p25", w5)
        self.assertIn("p75", w5)
        self.assertIn("min", w5)
        self.assertIn("max", w5)
        self.assertIn("first_date", w5)
        self.assertIn("last_date", w5)
        self.assertEqual(w5["n_eps_records"], 4)
        self.assertTrue(v["pe_5yr_evaluable"])
        self.assertEqual(v["pe_5yr_evaluability_reason"], "ok")
        w10 = basis["windows"]["10yr"]
        self.assertEqual(w10["window_bars"], 1300)  # fixture is shorter than 2520
        self.assertTrue(v["pe_10yr_evaluable"])

    def test_low_coverage_fixture_is_not_evaluable(self):
        from scripts import build_snapshot as bs
        import datetime as _dt
        # Earnings history starts AFTER the price window -- primary guard 3
        # (EPS series validity) must withhold trust even though a median is
        # still computed from the few usable bars.
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                 for d in ("2023-01-01", "2023-04-01", "2023-07-01", "2023-10-01")]
        day = _dt.date(2000, 1, 1)
        rows = []
        for i in range(1300):
            rows.append({"date": day.isoformat(), "close": 40.0,
                        "adjusted_close": 20.0})
            day += _dt.timedelta(days=1)
        v = bs.build_valuation({"last": 100.0}, {}, {}, rows=rows, earn_q=earn_q)
        self.assertFalse(v["pe_5yr_evaluable"])
        self.assertIn("insufficient_eps_series", v["pe_5yr_evaluability_reason"])


_QC12_AAPL_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                 "stock-analysis/AAPL/2026-08-07-refresh/"
                 "detail_reports_2026-08-07/raw")
_QC12_MU_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
               "stock-analysis/MU/2026-08-08/detail_reports_2026-08-08/raw")


@unittest.skipUnless(os.path.isdir(_QC12_AAPL_RAW) and os.path.isdir(_QC12_MU_RAW),
                     "QC12 ground-truth read-only bundles not mounted on this machine")
class TestQC12RealGroundTruth(unittest.TestCase):
    """Regression-pins the EXACT measured ground truth from the real AAPL/MU
    raw bundles (docs/QC_REMEDIATION_TRACKER.md QC12): trailing bars + raw
    close + as-filed EPS. Guarded -- SKIPPED where the read-only bundle volume
    isn't mounted; never approximated or fabricated inline (these are real
    6700+ row daily histories, too large to embed as a fixture)."""

    @staticmethod
    def _load(raw_dir):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(raw_dir, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        earnings = bs.load_raw(os.path.join(raw_dir, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        return rows, earn_q

    def test_aapl_ground_truth(self):
        from scripts import build_snapshot as bs
        rows, earn_q = self._load(_QC12_AAPL_RAW)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        w10 = bs._pe_window_stats(rows, earn_q, bs._TEN_YR_ROWS)
        # 5yr window has NO split inside it -- D2/D3 must be a no-op here
        # (unchanged from the original QC12 ground truth).
        self.assertAlmostEqual(w5["median"], 30.7333, places=4)
        self.assertAlmostEqual(w5["p25"], 27.6502, places=4)
        self.assertAlmostEqual(w5["p75"], 34.5221, places=4)
        self.assertEqual(w5["n_points"], 1260)
        self.assertEqual(w5["n_skipped"], 0)
        self.assertTrue(w5["evaluable"])
        self.assertEqual(w5["n_splits_in_window"], 0)
        # D2/D3: the 10yr window DOES cross AAPL's 2020-08-31 4:1 split.
        # BEFORE this fix (raw close paired with reportedEPS, which the
        # vendor DOES retroactively restate onto the current share basis):
        # median 36.4691, p25 30.2025, p75 68.8014 -- all inflated by ~2
        # years of pre-split bars whose P/E was overstated ~4x. AFTER
        # (split-adjusted close): median 27.1935, p25 18.3771, p75 32.7906.
        self.assertAlmostEqual(w10["median"], 27.1935, places=4)
        self.assertAlmostEqual(w10["p25"], 18.3771, places=4)
        self.assertAlmostEqual(w10["p75"], 32.7906, places=4)
        self.assertEqual(w10["n_points"], 2520)
        self.assertEqual(w10["n_skipped"], 0)
        self.assertTrue(w10["evaluable"])
        self.assertEqual(w10["n_splits_in_window"], 1)
        self.assertEqual(w10["splits_in_window"],
                         [{"date": "2020-08-31", "coefficient": 4.0}])

    def test_mu_ground_truth(self):
        from scripts import build_snapshot as bs
        rows, earn_q = self._load(_QC12_MU_RAW)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        w10 = bs._pe_window_stats(rows, earn_q, bs._TEN_YR_ROWS)
        self.assertAlmostEqual(w5["median"], 17.8893, places=4)
        self.assertAlmostEqual(w5["p25"], 9.8168, places=4)
        self.assertAlmostEqual(w5["p75"], 27.0563, places=4)
        self.assertEqual(w5["n_points"], 947)
        self.assertEqual(w5["n_skipped"], 313)
        self.assertTrue(w5["evaluable"])
        self.assertAlmostEqual(w10["median"], 15.6144, places=4)
        self.assertAlmostEqual(w10["p25"], 6.9053, places=4)
        self.assertAlmostEqual(w10["p75"], 25.2907, places=4)
        self.assertEqual(w10["n_points"], 2207)
        self.assertEqual(w10["n_skipped"], 313)
        self.assertTrue(w10["evaluable"])

    def test_shipped_defect_values_no_longer_produced(self):
        from scripts import build_snapshot as bs
        rows_mu, earn_q_mu = self._load(_QC12_MU_RAW)
        w5_mu = bs._pe_window_stats(rows_mu, earn_q_mu, bs._FIVE_YR_ROWS)
        self.assertNotAlmostEqual(w5_mu["median"], 1.947830096821801, places=2)
        w10_mu = bs._pe_window_stats(rows_mu, earn_q_mu, bs._TEN_YR_ROWS)
        self.assertNotAlmostEqual(w10_mu["median"], 1.3660492922362764, places=2)
        rows_a, earn_q_a = self._load(_QC12_AAPL_RAW)
        w5_a = bs._pe_window_stats(rows_a, earn_q_a, bs._FIVE_YR_ROWS)
        self.assertNotAlmostEqual(w5_a["median"], 21.198563518380485, places=2)
        w10_a = bs._pe_window_stats(rows_a, earn_q_a, bs._TEN_YR_ROWS)
        self.assertNotAlmostEqual(w10_a["median"], 15.223384281529322, places=2)


# --------------------------------------------------------------------------- #
# D2+D3: the QC12/QC2 "raw close is never split-adjusted" premise was FALSE.
# reportedEPS IS retroactively restated onto the CURRENT share basis by the
# vendor (AAPL FQ2014-03-31 reportedEPS=0.415 == as-filed $11.62 / 28, the
# cumulative 7:1 (2014) x 4:1 (2020) split factor) -- pairing it with RAW
# close inflates every pre-split bar by the split ratio. The fix: split-adjust
# (never dividend-adjust) close/high/low using the vendor's own per-bar
# "8. split coefficient" before pairing with reportedEPS / building the
# 52wk range.
# --------------------------------------------------------------------------- #

class TestSplitCoefficientParsing(unittest.TestCase):
    """parse_daily_rows carries a per-row split_coefficient (+ provenance of
    where it came from) so downstream construction can split-adjust WITHOUT
    dividend-adjusting (adjusted_close is both split- AND dividend-adjusted,
    which would depress historical P/E on high-yield names)."""

    def test_av_json_row_carries_real_split_coefficient(self):
        from scripts import build_snapshot as bs
        payload = {"Time Series (Daily)": {
            "2020-08-31": {"1. open": "127.58", "2. high": "131.0",
                           "3. low": "126.0", "4. close": "129.04",
                           "5. adjusted close": "125.17679043500442",
                           "6. volume": "223505733",
                           "7. dividend amount": "0.0000",
                           "8. split coefficient": "4.0"},
        }}
        rows = bs.parse_daily_rows(payload)
        self.assertEqual(rows[0]["split_coefficient"], 4.0)
        self.assertEqual(rows[0]["split_coefficient_source"], "av")

    def test_av_json_row_defaults_to_no_split_when_no_split_occurred(self):
        from scripts import build_snapshot as bs
        payload = {"Time Series (Daily)": {
            "2020-09-01": {"1. open": "132.76", "2. high": "134.8",
                           "3. low": "130.53", "4. close": "134.18",
                           "5. adjusted close": "130.16290871488604",
                           "6. volume": "152470142",
                           "7. dividend amount": "0.0000",
                           "8. split coefficient": "1.0"},
        }}
        rows = bs.parse_daily_rows(payload)
        self.assertEqual(rows[0]["split_coefficient"], 1.0)
        self.assertEqual(rows[0]["split_coefficient_source"], "av")

    def test_av_json_row_missing_split_column_defaults_1_and_is_disclosed(self):
        # Real finding (GOOG archived bundle, 2026-08-07-refresh): some AV
        # daily_adjusted.json fetches carry only 6 OHLCV keys, no "7./8."
        # dividend/split columns at all -- a DIFFERENT vendor response shape,
        # not "no split happened". Defaulting to 1.0 is the only safe
        # arithmetic choice (never fabricate a coefficient), but the
        # degradation must be disclosed, never silent.
        from scripts import build_snapshot as bs
        payload = {"Time Series (Daily)": {
            "2022-07-18": {"1. open": "113.44", "2. high": "114.8",
                           "3. low": "109.3", "4. close": "109.91",
                           "5. adjusted close": "108.94392795911916",
                           "6. volume": "33247345"},
        }}
        rows = bs.parse_daily_rows(payload)
        self.assertEqual(rows[0]["split_coefficient"], 1.0)
        self.assertEqual(rows[0]["split_coefficient_source"], "av_missing")

    def test_stooq_csv_rows_default_split_coefficient_1_stooq_source(self):
        # stooq's close is ALREADY split-adjusted (SERIES_SOURCE_STOOQ), so a
        # constant 1.0 (no further adjustment) is correct BY CONSTRUCTION --
        # unlike the av_missing case, this is not a degradation, but the
        # provenance is still disclosed distinctly.
        from scripts import build_snapshot as bs
        csv_text = "Date,Open,High,Low,Close,Volume\n2026-01-02,10,11,9,10.5,1000\n"
        rows = bs.parse_daily_rows(csv_text)
        self.assertEqual(rows[0]["split_coefficient"], 1.0)
        self.assertEqual(rows[0]["split_coefficient_source"], "stooq")


class TestSplitFactorHelper(unittest.TestCase):
    """_split_factors: for each ascending row, the cumulative product of
    split_coefficient over all STRICTLY LATER rows -- so
    price_at_d / factor(d) rebases every historical bar onto the basis of the
    LAST row passed in (whatever that happens to be)."""

    def test_real_aapl_2020_split_convention(self):
        # Real AAPL bars (docs D2/D3): 2020-08-28 raw close 499.23, the
        # 2020-08-31 bar carries split_coefficient 4.0, no further AAPL split
        # since. split_factor(2020-08-28) must be 4.0 (the 08-31 coefficient
        # is STRICTLY AFTER 08-28); split_factor(2020-08-31) must be 1.0 (its
        # OWN coefficient is excluded, nothing after it here).
        from scripts import build_snapshot as bs
        rows = [
            {"date": "2020-08-27", "close": 500.04, "split_coefficient": 1.0},
            {"date": "2020-08-28", "close": 499.23, "split_coefficient": 1.0},
            {"date": "2020-08-31", "close": 129.04, "split_coefficient": 4.0},
            {"date": "2020-09-01", "close": 134.18, "split_coefficient": 1.0},
        ]
        factors = bs._split_factors(rows)
        self.assertAlmostEqual(factors[1], 4.0)   # 2020-08-28
        self.assertAlmostEqual(factors[2], 1.0)   # 2020-08-31 (split day itself)
        self.assertAlmostEqual(factors[3], 1.0)   # 2020-09-01
        adjusted_0828 = rows[1]["close"] / factors[1]
        self.assertAlmostEqual(adjusted_0828, 124.8075, places=4)
        # The split day's own close must be UNCHANGED by the adjustment.
        adjusted_0831 = rows[2]["close"] / factors[2]
        self.assertAlmostEqual(adjusted_0831, 129.04)

    def test_missing_or_absent_coefficient_defaults_to_no_split(self):
        from scripts import build_snapshot as bs
        rows = [{"date": "2020-01-01", "close": 100.0},  # no key at all
                {"date": "2020-01-02", "close": 101.0, "split_coefficient": None}]
        factors = bs._split_factors(rows)
        self.assertEqual(factors, [1.0, 1.0])

    def test_two_splits_compound(self):
        from scripts import build_snapshot as bs
        rows = [
            {"date": "2000-01-01", "close": 100.0, "split_coefficient": 1.0},
            {"date": "2010-01-01", "close": 50.0, "split_coefficient": 2.0},
            {"date": "2020-01-01", "close": 25.0, "split_coefficient": 2.0},
            {"date": "2026-01-01", "close": 12.5, "split_coefficient": 1.0},
        ]
        factors = bs._split_factors(rows)
        # Bar 0 sits before BOTH splits -> current-basis factor is 2*2=4.
        self.assertAlmostEqual(factors[0], 4.0)
        # Bar 1 IS the first split's day (excluded); only the SECOND split
        # (2020) is strictly after it -> factor 2.0.
        self.assertAlmostEqual(factors[1], 2.0)
        self.assertAlmostEqual(factors[2], 1.0)
        self.assertAlmostEqual(factors[3], 1.0)


class TestQC12PeSeriesSplitContinuity(unittest.TestCase):
    """rolling_ttm_pe_series must be CONTINUOUS across a split -- this is the
    load-bearing D2 regression: the pre-fix construction paired RAW close
    (never split-adjusted) with reportedEPS (which the vendor DOES restate
    onto the current share basis), producing a P/E series that steps by the
    split ratio at the split date (AAPL's real 10yr series: 151.28 -> 39.10
    across 2020-08-28 -> 2020-08-31, a ~3.9x step for a 4:1 split)."""

    def test_pe_continuous_across_synthetic_4to1_split(self):
        from scripts import build_snapshot as bs
        # TTM EPS constant at 4.0 throughout (4 quarters of $1.00, well
        # before the price window) so any P/E discontinuity can only come
        # from the price side.
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                  for d in ("2018-01-01", "2018-04-01", "2018-07-01", "2018-10-01")]
        rows = [
            {"date": "2020-08-27", "close": 500.04, "split_coefficient": 1.0},
            {"date": "2020-08-28", "close": 499.23, "split_coefficient": 1.0},
            {"date": "2020-08-31", "close": 129.04, "split_coefficient": 4.0},
            {"date": "2020-09-01", "close": 134.18, "split_coefficient": 1.0},
        ]
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        pes = dict(out["points"])
        # Pre-fix (raw close / 4.0 TTM EPS) would read ~124.8 vs ~32.3 --  a
        # near-4x step. Post-fix, split-adjusted close / 4.0 must be
        # CONTINUOUS: all four bars land within a few % of each other (the
        # underlying "true" price barely moved day to day).
        pe_values = [pes["2020-08-27"], pes["2020-08-28"],
                     pes["2020-08-31"], pes["2020-09-01"]]
        self.assertAlmostEqual(pe_values[1], 499.23 / 4.0 / 4.0, places=4)  # split-adj/EPS
        self.assertAlmostEqual(pe_values[2], 129.04 / 4.0, places=4)        # unchanged/EPS
        for a, b in zip(pe_values, pe_values[1:]):
            self.assertLess(abs(a - b) / b, 0.05, "P/E stepped across the split")

    def test_splits_in_window_disclosed(self):
        from scripts import build_snapshot as bs
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                  for d in ("2018-01-01", "2018-04-01", "2018-07-01", "2018-10-01")]
        rows = [
            {"date": "2020-08-28", "close": 499.23, "split_coefficient": 1.0,
             "split_coefficient_source": "av"},
            {"date": "2020-08-31", "close": 129.04, "split_coefficient": 4.0,
             "split_coefficient_source": "av"},
        ]
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["n_splits"], 1)
        self.assertEqual(out["splits"], [{"date": "2020-08-31", "coefficient": 4.0}])

    def test_no_splits_in_window_is_empty(self):
        from scripts import build_snapshot as bs
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                  for d in ("2018-01-01", "2018-04-01", "2018-07-01", "2018-10-01")]
        rows = [{"date": "2020-08-28", "close": 100.0, "split_coefficient": 1.0}]
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["n_splits"], 0)
        self.assertEqual(out["splits"], [])

    def test_degraded_bars_disclosed(self):
        from scripts import build_snapshot as bs
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                  for d in ("2018-01-01", "2018-04-01", "2018-07-01", "2018-10-01")]
        rows = [
            {"date": "2020-08-28", "close": 100.0,
             "split_coefficient": 1.0, "split_coefficient_source": "av_missing"},
            {"date": "2020-08-29", "close": 101.0,
             "split_coefficient": 1.0, "split_coefficient_source": "av"},
        ]
        out = bs.rolling_ttm_pe_series(rows, earn_q)
        self.assertEqual(out["split_degraded_bars"], 1)


class TestQC2Wk52SplitContinuity(unittest.TestCase):
    """_derive_52wk_range must never span two price scales the way the
    shipped defect did: AAPL anchored 2021-01-04 derived wk52_high 515.14
    (pre-4:1-split raw print) alongside wk52_low 103.10 (post-split) -- one
    'range' mixing two share-count bases, silently, because raw high/low
    were never split-adjusted."""

    def test_high_low_continuous_across_synthetic_split(self):
        from scripts import build_snapshot as bs
        import datetime as _dt
        anchor = _dt.date(2021, 1, 4)
        rows = []
        # ~11 months of flat pre-split trading at a ~500-ish level, then a
        # 4:1 split, then ~4 months of flat post-split trading at ~125 --
        # the "true" (split-adjusted) price barely moves throughout.
        day = anchor - _dt.timedelta(days=340)
        pre_split = True
        while day <= anchor:
            if day == _dt.date(2020, 8, 31):
                pre_split = False
                rows.append({"date": day.isoformat(), "high": 131.0, "low": 126.0,
                            "close": 129.04, "split_coefficient": 4.0,
                            "split_coefficient_source": "av"})
            elif pre_split:
                rows.append({"date": day.isoformat(), "high": 505.0, "low": 495.0,
                            "close": 500.0, "split_coefficient": 1.0,
                            "split_coefficient_source": "av"})
            else:
                rows.append({"date": day.isoformat(), "high": 132.0, "low": 123.0,
                            "close": 128.0, "split_coefficient": 1.0,
                            "split_coefficient_source": "av"})
            day += _dt.timedelta(days=1)
        high, low, basis = bs._derive_52wk_range(rows, None, None)
        # Pre-fix: high ~505 (raw pre-split) and low ~123 (raw post-split) --
        # a >4x spread from mixed bases. Post-fix: both bases are rebased to
        # the LAST (post-split) row, so high/low must sit within a tight,
        # single-scale band (~123-132), never the raw pre-split ~500s.
        self.assertLess(high, 140.0)
        self.assertGreater(low, 100.0)
        self.assertEqual(basis["n_splits_in_window"], 1)
        self.assertEqual(basis["splits_in_window"],
                         [{"date": "2020-08-31", "coefficient": 4.0}])


class TestQC12PriceBasisNoteCorrected(unittest.TestCase):
    """D2/D3: the pre-fix _PE_PRICE_BASIS_NOTE asserted a FALSE premise
    ("reportedEPS is never retroactively split-adjusted") -- falsified by
    AAPL's real FQ2014-03-31 reportedEPS=0.415 (as-filed $11.62 / 28x
    cumulative splits). The corrected note must describe split-adjustment,
    never claim reportedEPS is split-invariant, and price_basis must say so
    truthfully (not the old bare "raw close")."""

    def test_note_no_longer_asserts_the_false_premise(self):
        from scripts import build_snapshot as bs
        note = bs._PE_PRICE_BASIS_NOTE.lower()
        self.assertIn("split", note)
        self.assertNotIn("never retroactively split-adjusted", note)

    def test_price_basis_label_reflects_split_adjustment(self):
        from scripts import build_snapshot as bs
        import datetime as _dt
        earn_q = [{"reportedDate": d, "reportedEPS": "1.0"}
                 for d in ("1990-01-01", "1990-04-01", "1990-07-01", "1990-10-01")]
        day = _dt.date(2000, 1, 1)
        rows = []
        for i in range(1300):
            rows.append({"date": day.isoformat(), "close": 40.0 + (i % 5),
                        "adjusted_close": 20.0 + (i % 5), "split_coefficient": 1.0})
            day += _dt.timedelta(days=1)
        v = bs.build_valuation({"last": 100.0}, {}, {}, rows=rows, earn_q=earn_q)
        basis = v["pe_median_basis"]
        self.assertIn("split", basis["price_basis"].lower())
        self.assertNotIn("raw close", basis["price_basis"].lower())


_D2D3_NVDA_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                  "stock-analysis/NVDA/2026-08-02/detail_reports_2026-08-02/raw")
_D2D3_GOOG_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                  "stock-analysis/GOOG/2026-08-07-refresh/detail_reports_2026-08-07/raw")
_D2D3_TSLA_RAW = ("/Volumes/OWC-2TB/dev/kurama-data/Finance/portfolio/"
                  "stock-analysis/TSLA/2026-08-02/detail_reports_2026-07-31/raw")


@unittest.skipUnless(os.path.isdir(_QC12_AAPL_RAW) and os.path.isdir(_QC12_MU_RAW),
                     "QC12 ground-truth read-only bundles not mounted on this machine")
class TestD2D3RealGroundTruthRegression(unittest.TestCase):
    """AAPL/MU are the calibration bundles: NEITHER has a split inside its
    5yr window (AAPL's only split in the full history, 2020-08-31 4:1, sits
    inside the 10yr window but NOT the 5yr window), so the split-adjustment
    fix must be a no-op for pe_5yr_median and the wk52 range on both names."""

    @staticmethod
    def _load(raw_dir):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(raw_dir, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        earnings = bs.load_raw(os.path.join(raw_dir, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        return rows, earn_q

    def test_aapl_pe_5yr_median_unchanged(self):
        from scripts import build_snapshot as bs
        rows, earn_q = self._load(_QC12_AAPL_RAW)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        self.assertAlmostEqual(w5["median"], 30.7333, places=4)
        self.assertEqual(w5["n_splits_in_window"], 0)

    def test_mu_pe_5yr_median_unchanged(self):
        from scripts import build_snapshot as bs
        rows, earn_q = self._load(_QC12_MU_RAW)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        self.assertAlmostEqual(w5["median"], 17.8893, places=4)
        self.assertEqual(w5["n_splits_in_window"], 0)

    def test_aapl_pe_10yr_median_no_longer_split_contaminated(self):
        # The 10yr window DOES cross AAPL's 2020-08-31 4:1 split -- the
        # pre-fix median (36.4691) was contaminated by ~2 years of
        # 4x-inflated pre-split P/E points; post-fix it must land materially
        # LOWER, close to the 5yr median's regime (~30), never the shipped
        # contaminated value.
        from scripts import build_snapshot as bs
        rows, earn_q = self._load(_QC12_AAPL_RAW)
        w10 = bs._pe_window_stats(rows, earn_q, bs._TEN_YR_ROWS)
        self.assertEqual(w10["n_splits_in_window"], 1)
        self.assertNotAlmostEqual(w10["median"], 36.4691, places=2)
        self.assertTrue(w10["evaluable"])

    def test_aapl_wk52_range_unchanged(self):
        from scripts import build_snapshot as bs
        rows, _ = self._load(_QC12_AAPL_RAW)
        high, low, basis = bs._derive_52wk_range(rows, None, None)
        self.assertAlmostEqual(low, 216.58, places=2)
        self.assertAlmostEqual(high, 344.5699, places=2)
        self.assertEqual(basis["n_splits_in_window"], 0)

    def test_mu_wk52_range_unchanged(self):
        from scripts import build_snapshot as bs
        rows, _ = self._load(_QC12_MU_RAW)
        high, low, basis = bs._derive_52wk_range(rows, None, None)
        self.assertAlmostEqual(low, 111.67, places=2)
        self.assertAlmostEqual(high, 1255.00, places=2)
        self.assertEqual(basis["n_splits_in_window"], 0)


@unittest.skipUnless(os.path.isdir(_D2D3_NVDA_RAW), "NVDA archived bundle not mounted")
class TestD2D3NvdaOutOfSample(unittest.TestCase):
    """NVDA: real 10:1 split 2024-06-10 sits inside BOTH the 5yr and 10yr
    windows -- the shipped defect's worst case (measured shipped 5yr median
    365.98). Post-fix the median must fall into a plausible band."""

    def test_5yr_median_no_longer_split_contaminated(self):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(_D2D3_NVDA_RAW, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        earnings = bs.load_raw(os.path.join(_D2D3_NVDA_RAW, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        self.assertGreaterEqual(w5["n_splits_in_window"], 1)
        self.assertNotAlmostEqual(w5["median"], 365.9815950920245, places=1)
        self.assertLess(w5["median"], 150.0)


@unittest.skipUnless(os.path.isdir(_D2D3_TSLA_RAW), "TSLA archived bundle not mounted")
class TestD2D3TslaOutOfSample(unittest.TestCase):
    """TSLA: real 5:1 (2020-08-31) and 3:1 (2022-08-25) splits both sit
    inside the 10yr window; the 3:1 sits inside the 5yr window too (shipped
    5yr median 140.92 -- outside the plausible band)."""

    def test_5yr_median_no_longer_split_contaminated(self):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(_D2D3_TSLA_RAW, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        earnings = bs.load_raw(os.path.join(_D2D3_TSLA_RAW, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        self.assertGreaterEqual(w5["n_splits_in_window"], 1)
        self.assertNotAlmostEqual(w5["median"], 140.92217261904761, places=1)


@unittest.skipUnless(os.path.isdir(_D2D3_GOOG_RAW), "GOOG archived bundle not mounted")
class TestD2D3GoogDegradedSplitData(unittest.TestCase):
    """GOOG (2026-08-07-refresh archived bundle): the raw daily_adjusted.json
    carries only 6 OHLCV keys -- no '7. dividend amount' / '8. split
    coefficient' columns at all, a genuinely different vendor response shape
    (NOT "no split happened": GOOG's real 20:1 split, 2022-07-18, sits
    inside the 5yr window). The fix cannot invent a coefficient the payload
    never carried -- it must DISCLOSE the degradation loudly rather than
    silently leave the contamination in place."""

    def test_missing_split_column_is_disclosed_not_silent(self):
        from scripts import build_snapshot as bs
        daily = bs.load_daily_raw(os.path.join(_D2D3_GOOG_RAW, "daily_adjusted.json"))
        rows = bs.parse_daily_rows(daily)
        self.assertTrue(all(r["split_coefficient_source"] == "av_missing" for r in rows))
        earnings = bs.load_raw(os.path.join(_D2D3_GOOG_RAW, "earnings.json"))
        earn_q = bs.load_quarterly_earnings(earnings)
        w5 = bs._pe_window_stats(rows, earn_q, bs._FIVE_YR_ROWS)
        # The window is entirely degraded-source bars -> loudly disclosed.
        self.assertGreater(w5["split_degraded_bars"], 0)


if __name__ == "__main__":
    unittest.main()
