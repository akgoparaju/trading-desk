"""Indicator math library for the trading-desk plugin.

Pure-function, stdlib-only (math, statistics) technical-indicator arithmetic.
This module is the ONLY place indicator arithmetic lives; the LLM layer never
does math. All price/value series are OLDEST-FIRST (values[-1] is most recent).

Trading-day windows: 1m=21, 3m=63, 6m=126, 12m=252. Annualization factor = sqrt(252).
"""

import math
import statistics

# Annualization factor for daily -> annual volatility scaling.
_ANNUALIZATION = math.sqrt(252)


def sma(values: list[float], n: int) -> float | None:
    """Simple moving average of the last ``n`` values.

    Formula: mean(values[-n:]) = sum(values[-n:]) / n.
    Returns None if fewer than ``n`` values are available.
    """
    if n <= 0 or len(values) < n:
        return None
    window = values[-n:]
    return sum(window) / n


def sma_series(values: list[float], n: int) -> list[float]:
    """Rolling simple moving average.

    Formula: out[i] = mean(values[i : i+n]) for each window of length n.
    Result length = len(values) - n + 1 (empty list if insufficient data).
    """
    if n <= 0 or len(values) < n:
        return []
    out = []
    window_sum = sum(values[:n])
    out.append(window_sum / n)
    for i in range(n, len(values)):
        window_sum += values[i] - values[i - n]
        out.append(window_sum / n)
    return out


def ema_series(values: list[float], n: int) -> list[float]:
    """Exponential moving average series.

    Seed = SMA of the first ``n`` values; smoothing k = 2 / (n + 1).
    Recursion: ema_t = value_t * k + ema_{t-1} * (1 - k).
    Result length = len(values) - n + 1 (empty list if insufficient data).
    """
    if n <= 0 or len(values) < n:
        return []
    k = 2 / (n + 1)
    seed = sum(values[:n]) / n
    out = [seed]
    prev = seed
    for i in range(n, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out.append(prev)
    return out


def rsi(values: list[float], n: int = 14) -> float | None:
    """Relative Strength Index using Wilder smoothing.

    Deltas computed across the whole series. Seed avg_gain/avg_loss = simple
    mean of the FIRST n deltas, then Wilder recursion:
        avg = (avg * (n - 1) + current) / n.
    RS = avg_gain / avg_loss; RSI = 100 - 100 / (1 + RS).
    All-gain -> 100.0; all-loss -> 0.0. Returns None if len(values) < n + 1.
    """
    if n <= 0 or len(values) < n + 1:
        return None
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0.0 for d in deltas]
    losses = [-d if d < 0 else 0.0 for d in deltas]

    avg_gain = sum(gains[:n]) / n
    avg_loss = sum(losses[:n]) / n
    for i in range(n, len(deltas)):
        avg_gain = (avg_gain * (n - 1) + gains[i]) / n
        avg_loss = (avg_loss * (n - 1) + losses[i]) / n

    if avg_loss == 0:
        # Pure gains -> 100; perfectly flat (no gains, no losses) -> neutral 50.
        return 100.0 if avg_gain > 0 else 50.0
    if avg_gain == 0:
        return 0.0
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def macd(values: list[float]) -> dict | None:
    """Moving Average Convergence Divergence (12/26/9).

    macd_line_series = ema12_series - ema26_series, aligned on their TAILS
    (both end at the last bar). signal = last value of EMA9 over macd_line_series.
    hist = macd - signal. Returns {"macd", "signal", "hist"} (last values),
    or None if len(values) < 35.
    """
    if len(values) < 35:
        return None
    ema12 = ema_series(values, 12)
    ema26 = ema_series(values, 26)
    # Tail-align: ema26 is shorter; take the matching tail of ema12.
    k = len(ema26)
    ema12_tail = ema12[-k:]
    macd_line = [ema12_tail[i] - ema26[i] for i in range(k)]
    signal_series = ema_series(macd_line, 9)
    macd_val = macd_line[-1]
    signal_val = signal_series[-1]
    return {"macd": macd_val, "signal": signal_val, "hist": macd_val - signal_val}


def pct_return(values: list[float], lookback: int) -> float | None:
    """Percentage return over ``lookback`` bars.

    Formula: values[-1] / values[-1 - lookback] - 1.
    Returns None if there is not enough data or the base value is zero.
    """
    if lookback <= 0 or len(values) < lookback + 1:
        return None
    base = values[-1 - lookback]
    if base == 0:
        return None
    return values[-1] / base - 1


def log_returns(values: list[float]) -> list[float]:
    """Daily log returns.

    Formula: r_t = ln(values[t] / values[t-1]) for t = 1..len-1.
    Result length = len(values) - 1 (empty if fewer than 2 values).
    """
    if len(values) < 2:
        return []
    return [math.log(values[i] / values[i - 1]) for i in range(1, len(values))]


def realized_vol(values: list[float], n: int) -> float | None:
    """Annualized realized volatility.

    Formula: statistics.stdev(last n log-returns) * sqrt(252).
    Requires len(values) >= n + 1 (so at least n log-returns exist), else None.
    """
    if n < 2 or len(values) < n + 1:
        return None
    rets = log_returns(values)
    window = rets[-n:]
    return statistics.stdev(window) * _ANNUALIZATION


def realized_vol_ex_earnings(closes, dates, earnings_dates, n) -> float | None:
    """Annualized realized volatility with earnings-print days masked out.

    Wave 4B: the contaminated ``realized_vol`` includes the big print-day jump,
    which inflates the "realized" number the vol gate compares IV against. This
    strips it. ``closes`` and ``dates`` are PARALLEL oldest-first lists (one date
    per close). A log-return r_t = ln(close_t / close_{t-1}) is attributed to day
    ``dates[t]`` (the return-realizing day). A return is MASKED (dropped) when its
    day falls within +/-1 TRADING SESSION of any earnings date -- i.e. the day
    itself, the trading day immediately before, or the trading day immediately
    after any date in ``earnings_dates``. "Trading session" here means adjacency
    in the ``dates`` list (index +/-1), not calendar days, so weekends/holidays
    do not widen the window.

    ANNUALIZATION CONVENTION (documented Philosophy-A choice): the surviving
    returns are annualized by ``sqrt(252)`` UNCONDITIONALLY -- the stripped days
    are treated as NON-EVENTS (removed noise), not as missing calendar time. We
    do NOT rescale the annualization factor for the dropped count.

    Uses the last ``n`` SURVIVING returns (the window is applied AFTER masking).
    Returns None when ``n < 2``, the input lists are misaligned/too short, or
    fewer than ``n`` unmasked returns remain.
    """
    if n < 2:
        return None
    if not isinstance(closes, list) or not isinstance(dates, list):
        return None
    if len(closes) != len(dates) or len(closes) < 2:
        return None

    earn = {str(d)[:10] for d in (earnings_dates or []) if d}

    # Indices in `dates` that are within +/-1 session of any earnings date.
    # A session is list adjacency, so we find each earnings date's position (or
    # the insertion neighborhood if the exact date is not a trading day) and mask
    # that index and its immediate list-neighbors.
    masked_days = set()
    date_to_idx = {d: i for i, d in enumerate(dates)}
    for ed in earn:
        if ed in date_to_idx:
            i = date_to_idx[ed]
            for j in (i - 1, i, i + 1):
                if 0 <= j < len(dates):
                    masked_days.add(dates[j])
        else:
            # Earnings date not a trading day (weekend/holiday): mask the trading
            # day immediately before and immediately after it in the series.
            before = None
            after = None
            for i, d in enumerate(dates):
                if d < ed:
                    before = i
                elif after is None:
                    after = i
                    break
            for j in (before, after):
                if j is not None:
                    masked_days.add(dates[j])

    # Build the surviving return series: r_t attributed to dates[t] (t = 1..).
    surviving = []
    for t in range(1, len(closes)):
        c0, c1 = closes[t - 1], closes[t]
        if c0 is None or c1 is None or c0 <= 0 or c1 <= 0:
            continue
        if dates[t] in masked_days:
            continue
        surviving.append(math.log(c1 / c0))

    if len(surviving) < n:
        return None
    window = surviving[-n:]
    return statistics.stdev(window) * _ANNUALIZATION


def beta_corr(stock: list[float], bench: list[float]) -> dict | None:
    """Beta and correlation of ``stock`` vs ``bench``.

    Align by taking the last k prices of each, k = min(len(stock), len(bench)).
    Compute daily log-returns, then:
        beta = covariance(stock_r, bench_r) / variance(bench_r)  (sample stats)
        corr = correlation(stock_r, bench_r)                     (Pearson)
        n_days = k - 1.
    Returns {"beta", "corr", "n_days"}, or None if k < 60.
    """
    k = min(len(stock), len(bench))
    if k < 60:
        return None
    s = stock[-k:]
    b = bench[-k:]
    s_r = log_returns(s)
    b_r = log_returns(b)
    var_b = statistics.variance(b_r)
    beta = statistics.covariance(s_r, b_r) / var_b
    corr = statistics.correlation(s_r, b_r)
    return {"beta": beta, "corr": corr, "n_days": k - 1}


def ma_slope(values: list[float], n: int, lookback: int = 20) -> float | None:
    """Slope of the SMA over a lookback window.

    Formula: (sma_now / sma_{lookback bars ago}) - 1, computed from sma_series.
    Returns None if the SMA series is too short or the earlier SMA is zero.
    """
    series = sma_series(values, n)
    if len(series) < lookback + 1:
        return None
    now = series[-1]
    prior = series[-1 - lookback]
    if prior == 0:
        return None
    return now / prior - 1


def max_drawdown(values: list[float]) -> float:
    """Maximum drawdown (most negative peak-to-trough return).

    Iterates tracking the running peak; drawdown_t = values[t] / peak - 1.
    Returns the most negative such value (<= 0.0). Empty/short series -> 0.0.
    """
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for v in values:
        if v > peak:
            peak = v
        if peak != 0:
            dd = v / peak - 1
            if dd < worst:
                worst = dd
    return worst


def drawdowns_by_year(rows: list[dict]) -> list[dict]:
    """Per-calendar-year maximum drawdown.

    ``rows`` are dicts with keys "date" ("YYYY-MM-DD") and "adjusted_close".
    Peak is tracked WITHIN each year only (resets at year boundaries).
    Returns [{"year": int, "max_dd": float}, ...] ordered by year ascending.
    """
    by_year: dict[int, list[float]] = {}
    for row in rows:
        year = int(row["date"][:4])
        by_year.setdefault(year, []).append(float(row["adjusted_close"]))
    out = []
    for year in sorted(by_year):
        out.append({"year": year, "max_dd": max_drawdown(by_year[year])})
    return out


def drawdown_episodes(values: list[float], threshold: float) -> int:
    """Count distinct peak-to-trough declines of at least ``threshold``.

    Iterate tracking the running peak. When value < peak * (1 - threshold) and
    not already in an episode -> count it and enter the episode. A new episode
    is only possible after full recovery to (or above) the prior peak, which
    also advances the peak. Returns the episode count.
    """
    if not values:
        return 0
    peak = values[0]
    in_episode = False
    count = 0
    for v in values:
        if v >= peak:
            peak = v
            in_episode = False
        elif not in_episode and v < peak * (1 - threshold):
            count += 1
            in_episode = True
    return count


def ewma_halflife(pairs: list[tuple[float, float]]) -> float | None:
    """Half-life-decayed weighted mean of (value, age_days) observations.

    Each observation carries an age in days; its decay weight is
    ``0.5 ** (age_days / half_life)`` scaled by an optional per-observation
    relevance weight folded into ``pairs`` by the caller. Here ``pairs`` are
    ``(value, weight)`` tuples where ``weight`` already embeds both relevance
    and the half-life decay term. Returns ``sum(value*weight)/sum(weight)``,
    or None when the weight total is zero (no usable observations).

    Kept generic (weights precomputed by the caller) so the decay policy --
    half_life = 3 days for news_heat -- lives with the caller and this stays a
    pure weighted mean. See ``_news_heat`` in build_snapshot.py.
    """
    num = 0.0
    den = 0.0
    for value, weight in pairs:
        if value is None or weight is None or weight <= 0:
            continue
        num += value * weight
        den += weight
    if den <= 0:
        return None
    return num / den


def halflife_weight(age_days: float, half_life: float) -> float | None:
    """Exponential half-life decay weight: ``0.5 ** (age_days / half_life)``.

    age_days may be 0 (weight 1.0) but not negative; half_life must be > 0.
    Returns None on invalid input (negative age or non-positive half_life).
    """
    if half_life is None or half_life <= 0:
        return None
    if age_days is None or age_days < 0:
        return None
    return 0.5 ** (age_days / half_life)


def zscore(value: float, history: list[float]) -> float | None:
    """Standard z-score of ``value`` against ``history``: (value - mean)/stdev.

    Uses the SAMPLE standard deviation (statistics.stdev). Returns None when
    fewer than 5 history points are available (guard) or the stdev is zero
    (degenerate / constant series -> no meaningful spread).
    """
    if len(history) < 5:
        return None
    mu = statistics.mean(history)
    sd = statistics.stdev(history)
    if sd == 0:
        return None
    return (value - mu) / sd


def percentile_rank(value: float, history: list[float]) -> float | None:
    """Percentile rank of ``value`` within ``history``.

    Formula: 100 * (count of history <= value) / len(history).
    Returns None if len(history) < 10.
    """
    if len(history) < 10:
        return None
    at_or_below = sum(1 for h in history if h <= value)
    return 100 * at_or_below / len(history)


def dist_from_high(values: list[float]) -> float:
    """Distance of the latest value from the series high.

    Formula: values[-1] / max(values) - 1 (<= 0.0). Empty series -> 0.0.
    """
    if not values:
        return 0.0
    high = max(values)
    if high == 0:
        return 0.0
    return values[-1] / high - 1


def overnight_gap_series(rows: list[dict]) -> list[float]:
    """Overnight-gap series over oldest-first OHLCV rows.

    Each gap is ``adj_open[i] / adjusted_close[i-1] - 1`` -- the return from the
    prior day's ADJUSTED close to today's ADJUSTED open. The raw ``open`` is
    adjustment-consistent-ified by the day's split/dividend factor
    ``adjusted_close/close`` so a split/dividend does NOT manufacture a spurious
    gap (raw open vs adjusted prior close would blow up around any adjustment
    event -- real-data finding: BE showed a bogus 57.8% max gap / kurtosis 56
    from that mismatch). When the raw ``close`` is absent/zero (e.g. stooq CSV,
    where ``close`` already IS adjusted) the factor is 1 and ``open`` is used
    as-is. Rows whose ``open`` or the prior ``adjusted_close`` is absent/zero are
    SKIPPED. Result length <= len(rows) - 1; empty if fewer than 2 usable rows.
    """
    out = []
    for i in range(1, len(rows)):
        prev_adj = rows[i - 1].get("adjusted_close")
        cur_open = rows[i].get("open")
        cur_close = rows[i].get("close")
        cur_adj = rows[i].get("adjusted_close")
        if prev_adj is None or cur_open is None or prev_adj == 0:
            continue
        # Adjust the raw open by today's split/div factor so both sides of the
        # ratio live in the same (adjusted) price space.
        if cur_close and cur_adj is not None and cur_close != 0:
            adj_open = cur_open * (cur_adj / cur_close)
        else:
            adj_open = cur_open
        out.append(adj_open / prev_adj - 1)
    return out


def excess_kurtosis(values: list[float]) -> float | None:
    """Excess kurtosis: the 4th standardized moment minus 3.

    Formula (population moments):
        m2 = mean((x - mean)**2)
        m4 = mean((x - mean)**4)
        kurtosis = m4 / m2**2 ; excess = kurtosis - 3.
    A normal distribution has excess kurtosis 0; fat tails are positive.
    Returns None if fewer than 4 values or the variance is zero (degenerate).
    """
    n = len(values)
    if n < 4:
        return None
    mu = sum(values) / n
    m2 = sum((x - mu) ** 2 for x in values) / n
    if m2 == 0:
        return None
    m4 = sum((x - mu) ** 4 for x in values) / n
    return m4 / (m2 ** 2) - 3


def jump_count_2sigma(values: list[float]) -> int:
    """Count values whose absolute deviation exceeds 2x the population std.

    The 2-sigma threshold is a DOCUMENTED convention (not a calibrated
    parameter): ``count(|x| > 2 * std(values))`` where ``std`` is the
    population standard deviation of the series. Returns 0 for fewer than 2
    values or a zero-variance series (no jumps possible).
    """
    n = len(values)
    if n < 2:
        return 0
    mu = sum(values) / n
    var = sum((x - mu) ** 2 for x in values) / n
    if var <= 0:
        return 0
    std = math.sqrt(var)
    threshold = 2 * std
    return sum(1 for x in values if abs(x) > threshold)


# --------------------------------------------------------------------------- #
# Wave 4A: regime + institutional-level indicators (pure OHLCV, deterministic).
#
# These consume the FULL OHLCV ``rows`` (dicts with high/low/close/volume/date),
# not just the adjusted-close series, because trend-strength and money-flow are
# intraday-range measures. ADX in particular is a same-scale range measure and so
# uses the RAW ``close`` (not ``adjusted_close``): +DM/-DM and True Range must all
# live in the same price space, and mixing a raw high/low with an adjusted close
# would corrupt the ranges around any split/dividend event.
# --------------------------------------------------------------------------- #

def adx(rows: list[dict], n: int = 14) -> float | None:
    """Wilder's Average Directional Index (ADX) over OHLCV ``rows``.

    Standard Wilder construction on RAW high/low/close (a same-scale intraday
    range measure -- see module note):
      1. True Range  TR_t   = max(high-low, |high-prev_close|, |low-prev_close|)
      2. Directional movement (only the larger of the two moves counts; ties/neg
         -> 0):
            up   = high_t - high_{t-1};  down = low_{t-1} - low_t
            +DM_t = up   if (up > down and up > 0)   else 0
            -DM_t = down if (down > up and down > 0) else 0
      3. Wilder-smooth TR, +DM, -DM with the standard recursion
            sm_t = sm_{t-1} - sm_{t-1}/n + raw_t   (seed = sum of first n raws)
      4. +DI = 100 * sm(+DM)/sm(TR);  -DI = 100 * sm(-DM)/sm(TR)
         DX  = 100 * |+DI - -DI| / (+DI + -DI)   (0 when the DI sum is 0)
      5. ADX = Wilder-smoothed mean of DX: seed = mean of the first n DX values,
         then ADX_t = (ADX_{t-1}*(n-1) + DX_t)/n.

    Returns the latest ADX as a float, or None when there are fewer than
    ``2n + 1`` rows (n for the first smoothed DI, another n DX values to seed the
    ADX average, plus the extra prior-close bar). Rows must carry numeric
    high/low/close; a row missing any of them makes the whole computation None
    (ADX is only meaningful on a clean contiguous series).
    """
    if n <= 0 or len(rows) < 2 * n + 1:
        return None

    highs, lows, closes = [], [], []
    for r in rows:
        h, low, c = r.get("high"), r.get("low"), r.get("close")
        if h is None or low is None or c is None:
            return None
        highs.append(float(h))
        lows.append(float(low))
        closes.append(float(c))

    tr, plus_dm, minus_dm = [], [], []
    for i in range(1, len(rows)):
        hi_lo = highs[i] - lows[i]
        hi_pc = abs(highs[i] - closes[i - 1])
        lo_pc = abs(lows[i] - closes[i - 1])
        tr.append(max(hi_lo, hi_pc, lo_pc))
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)

    # len(tr) == len(rows) - 1 >= 2n. Wilder-smooth TR/+DM/-DM.
    def _wilder(series):
        smoothed = [sum(series[:n])]
        for i in range(n, len(series)):
            smoothed.append(smoothed[-1] - smoothed[-1] / n + series[i])
        return smoothed

    sm_tr = _wilder(tr)
    sm_plus = _wilder(plus_dm)
    sm_minus = _wilder(minus_dm)

    dx = []
    for i in range(len(sm_tr)):
        if sm_tr[i] == 0:
            dx.append(0.0)
            continue
        plus_di = 100 * sm_plus[i] / sm_tr[i]
        minus_di = 100 * sm_minus[i] / sm_tr[i]
        di_sum = plus_di + minus_di
        dx.append(100 * abs(plus_di - minus_di) / di_sum if di_sum != 0 else 0.0)

    if len(dx) < n:
        return None  # not enough DX values to seed the ADX average
    adx_val = sum(dx[:n]) / n
    for i in range(n, len(dx)):
        adx_val = (adx_val * (n - 1) + dx[i]) / n
    return adx_val


def ad_line(rows: list[dict]) -> list[float]:
    """Chaikin Accumulation/Distribution line (cumulative money-flow volume).

    Per bar:
        MFM = ((close - low) - (high - close)) / (high - low)   (0 when high==low)
        MFV = MFM * volume
    The A/D line is the running cumulative sum of MFV. Returns the FULL series
    (one point per usable row), OLDEST-first. Rows missing high/low/close/volume
    are skipped (they contribute no money-flow point). Empty input -> [].
    """
    out = []
    running = 0.0
    for r in rows:
        h, low, c, v = r.get("high"), r.get("low"), r.get("close"), r.get("volume")
        if h is None or low is None or c is None or v is None:
            continue
        h, low, c, v = float(h), float(low), float(c), float(v)
        if h == low:
            mfm = 0.0
        else:
            mfm = ((c - low) - (h - c)) / (h - low)
        running += mfm * v
        out.append(running)
    return out


def ad_line_slope(rows: list[dict], lookback: int = 20) -> float | None:
    """20-day slope of the Chaikin A/D line, VOLUME-normalized.

    The raw A/D line's magnitude scales with the ticker's share volume, so a bare
    ``ad[-1] - ad[-1-lookback]`` difference is not comparable across names. We
    normalize by the mean absolute per-bar money-flow volume proxy -- here the
    mean bar volume over the same window -- to express the slope in "average bars
    of one-sided accumulation" units. The SIGN (accumulation vs distribution) is
    the load-bearing signal; the magnitude is a comparable secondary read.

    Formula: (ad[-1] - ad[-1-lookback]) / (lookback * mean_bar_volume).
    Returns None when the A/D series is shorter than ``lookback + 1`` points or
    the volume normalizer is zero. Kept simple and documented (spec: "keep it
    simple + documented").
    """
    line = ad_line(rows)
    if lookback <= 0 or len(line) < lookback + 1:
        return None
    delta = line[-1] - line[-1 - lookback]
    vols = [float(r["volume"]) for r in rows
            if r.get("volume") is not None]
    if len(vols) < lookback:
        return None
    mean_vol = sum(vols[-lookback:]) / lookback
    denom = lookback * mean_vol
    if denom == 0:
        return None
    return delta / denom


def updown_volume(rows: list[dict], n: int = 50) -> float | None:
    """Up-day volume as a fraction of total volume over the last ``n`` bars.

    A bar is an "up day" when its close exceeds the PRIOR bar's close. Over the
    last ``n`` bars (each needing a prior bar for the comparison):
        ratio = sum(volume on up days) / sum(volume over all n days).
    Returns None when there are fewer than ``n + 1`` usable rows (need a prior
    close for the first of the n bars) or when total volume is zero. Rows missing
    close or volume break the contiguous window and yield None (the ratio must be
    over a clean n-bar window). Range is [0, 1].
    """
    if n <= 0 or len(rows) < n + 1:
        return None
    window = rows[-(n + 1):]  # n comparison bars + 1 prior-close anchor
    up_vol = 0.0
    total_vol = 0.0
    for i in range(1, len(window)):
        c = window[i].get("close")
        pc = window[i - 1].get("close")
        v = window[i].get("volume")
        if c is None or pc is None or v is None:
            return None
        v = float(v)
        total_vol += v
        if float(c) > float(pc):
            up_vol += v
    if total_vol == 0:
        return None
    return up_vol / total_vol


def anchored_vwap(rows: list[dict], anchor_date: str) -> float | None:
    """Anchored Volume-Weighted Average Price from ``anchor_date`` forward.

    Over all rows whose ``date`` is >= ``anchor_date`` (ISO ``YYYY-MM-DD``, so
    string comparison is chronological):
        typical_price = (high + low + close) / 3
        VWAP = sum(typical_price * volume) / sum(volume).
    Returns None when ``anchor_date`` is absent, no row falls on/after it, or the
    summed volume is zero. Rows missing high/low/close/volume are skipped. Uses
    RAW OHLC (an anchored cost-basis level lives in traded-price space, matching
    the raw close used elsewhere for same-scale range measures).
    """
    if not anchor_date:
        return None
    num = 0.0
    den = 0.0
    for r in rows:
        d = r.get("date")
        if d is None or d < anchor_date:
            continue
        h, low, c, v = r.get("high"), r.get("low"), r.get("close"), r.get("volume")
        if h is None or low is None or c is None or v is None:
            continue
        v = float(v)
        typical = (float(h) + float(low) + float(c)) / 3
        num += typical * v
        den += v
    if den == 0:
        return None
    return num / den
