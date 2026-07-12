from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Dict, List, Optional, Tuple

import pandas as pd

# Map yfinance-style index tickers to their ETF equivalents for Polygon compatibility.
BENCHMARK_MAP = {
    "^GSPC": "SPY",
    "^IXIC": "QQQ",
    "^DJI":  "DIA",
    "^RUT":  "IWM",
}

# Approximate threshold environment from the replay-mode defaults.
DEFAULT_THRESHOLDS: Dict[str, float] = {
    "first": 195.93,  # ~99
    "scnd": 117.11,   # ~90+
    "thrd": 99.04,    # ~70+
    "frth": 91.66,    # ~50+
    "ffth": 80.96,    # ~30+
    "sxth": 53.64,    # ~10+
    "svth": 24.86,    # ~1-
}


@dataclass
class RSResult:
    ticker: str
    benchmark: str
    last_close: float
    last_benchmark_close: float
    rs_line_last: float
    rs_score: float
    rs_rating: int


def _period_to_start(period: str) -> str:
    """Convert a period string like '2y' or '1y' to an ISO start date."""
    today = date.today()
    if period.endswith("y"):
        years = int(period[:-1])
        start = today.replace(year=today.year - years)
    elif period.endswith("mo"):
        months = int(period[:-2])
        year = today.year + (today.month - months - 1) // 12
        month = (today.month - months - 1) % 12 + 1
        start = today.replace(year=year, month=month)
    elif period.endswith("d"):
        start = today - timedelta(days=int(period[:-1]))
    else:
        raise ValueError(f"Unsupported period format: {period!r}. Use e.g. '2y', '6mo', '90d'.")
    return start.isoformat()


def _fetch_polygon_closes(
    ticker: str,
    start: str,
    end: str,
    api_key: str,
) -> Optional[pd.Series]:
    """Fetch daily adjusted close prices from Polygon for one ticker."""
    from polygon import RESTClient  # type: ignore

    client = RESTClient(api_key=api_key)
    aggs = client.get_aggs(
        ticker,
        1,
        "day",
        start,
        end,
        adjusted=True,
        sort="asc",
        limit=50000,
    )
    if not aggs:
        return None

    rows = {
        pd.Timestamp(a.timestamp, unit="ms", tz="UTC").tz_convert(None).normalize(): a.close
        for a in aggs
        if a.close is not None
    }
    if not rows:
        return None

    return pd.Series(rows, name=ticker)


def download_prices(ticker: str, benchmark: str, period: str) -> pd.DataFrame:
    """Download aligned daily close data for stock and benchmark via Polygon."""
    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        raise ValueError("POLYGON_API_KEY is not set. Add it to your .env file.")

    start = _period_to_start(period)
    end = date.today().isoformat()

    poly_benchmark = BENCHMARK_MAP.get(benchmark, benchmark)

    stock_close = _fetch_polygon_closes(ticker, start, end, api_key)
    bench_close = _fetch_polygon_closes(poly_benchmark, start, end, api_key)

    if stock_close is None or stock_close.empty:
        raise ValueError(f"No price data returned for {ticker}.")
    if bench_close is None or bench_close.empty:
        raise ValueError(f"No price data returned for benchmark {benchmark} ({poly_benchmark}).")

    df = pd.concat([stock_close.rename("stock_close"), bench_close.rename("benchmark_close")], axis=1).dropna()

    if len(df) < 70:
        raise ValueError(
            f"Not enough history after alignment ({len(df)} rows). Need at least 70 trading days."
        )

    return df


def safe_lookback_index(length: int, bars_back: int) -> int:
    """Match reference behavior when a symbol has limited history."""
    return min(bars_back, length - 1)


def weighted_perf_ratio(series: pd.Series) -> float:
    """Compute weighted 63/126/189/252-day performance."""
    n = len(series)
    i63 = safe_lookback_index(n, 63)
    i126 = safe_lookback_index(n, 126)
    i189 = safe_lookback_index(n, 189)
    i252 = safe_lookback_index(n, 252)

    last = float(series.iloc[-1])
    perf63 = last / float(series.iloc[-1 - i63])
    perf126 = last / float(series.iloc[-1 - i126])
    perf189 = last / float(series.iloc[-1 - i189])
    perf252 = last / float(series.iloc[-1 - i252])

    return 0.4 * perf63 + 0.2 * perf126 + 0.2 * perf189 + 0.2 * perf252


def attribute_percentile(
    total_rs_score: float,
    taller_perf: float,
    smaller_perf: float,
    range_up: int,
    range_dn: int,
    weight: float,
) -> int:
    """Approximate percentile mapping function."""
    adjusted = total_rs_score + (total_rs_score - smaller_perf) * weight
    if adjusted > taller_perf - 1:
        adjusted = taller_perf - 1

    k1 = smaller_perf / range_dn
    k2 = (taller_perf - 1) / range_up
    k3 = (k1 - k2) / (taller_perf - 1 - smaller_perf)

    rs_rating = adjusted / (k1 - k3 * (total_rs_score - smaller_perf))
    rs_rating = min(rs_rating, range_up)
    rs_rating = max(rs_rating, range_dn)
    return int(round(rs_rating))


def score_to_rating(score: float, thresholds: Dict[str, float]) -> int:
    """Map raw RS score to approximate 1-99 rating."""
    first = thresholds["first"]
    scnd = thresholds["scnd"]
    thrd = thresholds["thrd"]
    frth = thresholds["frth"]
    ffth = thresholds["ffth"]
    sxth = thresholds["sxth"]
    svth = thresholds["svth"]

    if score >= first:
        return 99
    if score <= svth:
        return 1
    if first > score >= scnd:
        return attribute_percentile(score, first, scnd, 98, 90, 0.33)
    if scnd > score >= thrd:
        return attribute_percentile(score, scnd, thrd, 89, 70, 2.1)
    if thrd > score >= frth:
        return attribute_percentile(score, thrd, frth, 69, 50, 0.0)
    if frth > score >= ffth:
        return attribute_percentile(score, frth, ffth, 49, 30, 0.0)
    if ffth > score >= sxth:
        return attribute_percentile(score, ffth, sxth, 29, 10, 0.0)
    if sxth > score >= svth:
        return attribute_percentile(score, sxth, svth, 9, 2, 0.0)
    return 1


def compute_rs(
    ticker: str,
    benchmark: str = "^GSPC",
    period: str = "2y",
) -> Tuple[RSResult, pd.DataFrame]:
    """Compute RS line, weighted RS score, and approximate RS rating."""
    df = download_prices(ticker, benchmark, period)
    df["rs_line"] = df["stock_close"] / df["benchmark_close"]

    stock_weighted = weighted_perf_ratio(df["stock_close"])
    benchmark_weighted = weighted_perf_ratio(df["benchmark_close"])
    rs_score = (stock_weighted / benchmark_weighted) * 100.0
    rs_rating = score_to_rating(rs_score, DEFAULT_THRESHOLDS)

    result = RSResult(
        ticker=ticker,
        benchmark=benchmark,
        last_close=float(df["stock_close"].iloc[-1]),
        last_benchmark_close=float(df["benchmark_close"].iloc[-1]),
        rs_line_last=float(df["rs_line"].iloc[-1]),
        rs_score=float(rs_score),
        rs_rating=int(rs_rating),
    )
    return result, df


@dataclass
class DailyResult:
    ticker: str
    target_date: str
    open: float
    close: float
    stock_return_pct: float
    benchmark_return_pct: float
    relative_return_pct: float  # stock - benchmark
    volume: float


def _prev_trading_day(target: date, lookback_days: int = 10) -> str:
    """Return a start date far enough back to guarantee we capture the prior trading day."""
    return (target - timedelta(days=lookback_days)).isoformat()


def _fetch_polygon_ohlcv(
    ticker: str,
    start: str,
    end: str,
    api_key: str,
) -> Optional[pd.DataFrame]:
    """Fetch daily OHLCV bars from Polygon for a date range."""
    from polygon import RESTClient  # type: ignore

    client = RESTClient(api_key=api_key)
    aggs = client.get_aggs(
        ticker, 1, "day", start, end,
        adjusted=True, sort="asc", limit=50000,
    )
    if not aggs:
        return None

    rows = [
        {
            "date": pd.Timestamp(a.timestamp, unit="ms", tz="UTC").tz_convert(None).normalize(),
            "open":   a.open,
            "close":  a.close,
            "volume": a.volume,
        }
        for a in aggs
        if None not in (a.open, a.close, a.volume)
    ]
    if not rows:
        return None

    df = pd.DataFrame(rows).set_index("date")
    return df


def compute_daily_performance(
    ticker: str,
    target_date: date,
    benchmark: str = "SPY",
    api_key: Optional[str] = None,
) -> DailyResult:
    """
    Compute a stock's return on target_date relative to the benchmark.
    Returns relative_return_pct = stock_return - benchmark_return.
    """
    key = api_key or os.environ.get("POLYGON_API_KEY", "")
    if not key:
        raise ValueError("POLYGON_API_KEY is not set.")

    start = _prev_trading_day(target_date)
    end = target_date.isoformat()

    stock_df = _fetch_polygon_ohlcv(ticker, start, end, key)
    bench_df = _fetch_polygon_ohlcv(benchmark, start, end, key)

    if stock_df is None or len(stock_df) < 2:
        raise ValueError(f"Not enough data for {ticker} around {end}.")
    if bench_df is None or len(bench_df) < 2:
        raise ValueError(f"Not enough benchmark data for {benchmark} around {end}.")

    target_ts = pd.Timestamp(target_date)
    if target_ts not in stock_df.index:
        raise ValueError(f"No data for {ticker} on {end} (market closed or bad date?).")
    if target_ts not in bench_df.index:
        raise ValueError(f"No benchmark data on {end}.")

    # Use the bar immediately before the target date as prior close
    stock_prior = float(stock_df["close"].iloc[-2])
    stock_close = float(stock_df.loc[target_ts, "close"])
    stock_open  = float(stock_df.loc[target_ts, "open"])
    stock_vol   = float(stock_df.loc[target_ts, "volume"])

    bench_prior = float(bench_df["close"].iloc[-2])
    bench_close = float(bench_df.loc[target_ts, "close"])

    stock_ret = (stock_close - stock_prior) / stock_prior * 100.0
    bench_ret = (bench_close - bench_prior) / bench_prior * 100.0

    return DailyResult(
        ticker=ticker,
        target_date=end,
        open=stock_open,
        close=stock_close,
        stock_return_pct=stock_ret,
        benchmark_return_pct=bench_ret,
        relative_return_pct=stock_ret - bench_ret,
        volume=stock_vol,
    )
