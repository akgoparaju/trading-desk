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
        q = [
            {"fiscalDateEnding": "2026-06-30", "reportedDate": "2026-07-01", "reportedEPS": "1.60"},
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
        self.assertEqual(m["schema_version"], "0.2.2")  # QF2: bumped 0.2.1 -> 0.2.2
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
        self.assertAlmostEqual(p["wk52_high"], 140.0)
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
        self.assertAlmostEqual(f["eps_ttm_computed"], 1.60 + 1.40 + 1.20 + 1.00)

    def test_net_cash(self):
        nc = self.snap["fundamentals"]["net_cash_defined"]
        self.assertAlmostEqual(nc["cash_st"], 9000)
        self.assertAlmostEqual(nc["lt_inv"], 2000)
        self.assertAlmostEqual(nc["total_debt"], 4000)
        self.assertAlmostEqual(nc["net"], 9000 + 2000 - 4000)

    def test_eps_ntm_method_is_nearest_fy(self):
        f = self.snap["fundamentals"]
        self.assertEqual(f["eps_ntm_method"], "nearest_future_fiscal_year")
        self.assertAlmostEqual(f["eps_ntm_consensus"], 7.50)

    def test_revisions_from_future_fy(self):
        rev = self.snap["fundamentals"]["revisions_90d"]
        self.assertAlmostEqual(rev["eps_now"], 7.50)
        self.assertAlmostEqual(rev["eps_90d_ago"], 7.00)
        self.assertAlmostEqual(rev["pct"], 7.50 / 7.00 - 1)
        self.assertAlmostEqual(rev["up_30d"], 9)
        self.assertAlmostEqual(rev["down_30d"], 3)
        nfy = self.snap["fundamentals"]["next_fy_consensus"]
        self.assertAlmostEqual(nfy["rev"], 34000)
        self.assertAlmostEqual(nfy["eps"], 7.50)

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
        self.assertIsNotNone(bm["beta"])
        self.assertIsNotNone(bm["corr"])
        self.assertIsNotNone(bm["spy_ret_1m"])
        self.assertGreaterEqual(bm["beta_n_days"], 60)

    def test_valuation(self):
        v = self.snap["valuation"]
        self.assertAlmostEqual(v["pe_ttm"], self.b.last / 6.0)
        self.assertAlmostEqual(v["pe_fwd"], self.b.last / 7.50)
        self.assertEqual(v["pe_median_method"], "approx_current_eps")
        self.assertIsNotNone(v["fcf_yield"])

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
        self.assertTrue(len(snap["meta"]["qc"]["checks"]) == 9)

    def test_gate_fails_on_corrupt_mktcap(self):
        with open(self.snap_path) as fh:
            snap = json.load(fh)
        snap["price"]["mktcap_overview"] *= 1.5
        with open(self.snap_path, "w") as fh:
            json.dump(snap, fh)
        proc = _run_gate(self.snap_path)
        self.assertEqual(proc.returncode, 1, proc.stdout)
        with open(self.snap_path) as fh:
            snap2 = json.load(fh)
        self.assertIs(snap2["meta"]["qc"]["passed"], False)

    def test_waive_flips_to_pass(self):
        with open(self.snap_path) as fh:
            snap = json.load(fh)
        snap["price"]["mktcap_overview"] *= 1.5
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


if __name__ == "__main__":
    unittest.main()
