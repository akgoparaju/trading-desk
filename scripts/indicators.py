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
