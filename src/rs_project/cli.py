from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

import requests

from .core import compute_rs, compute_daily_performance
from .plotting import plot_rs

FINVIZ_AUTH_TOKEN: str = os.environ.get("FINVIZ_AUTH_TOKEN", "")
FINVIZ_URL: str = os.environ.get("FINVIZ_URL", "")
FINVIZ_SMALLCAP_URL: str = os.environ.get("FINVIZ_SMALLCAP_URL", "")
FINVIZ_MIDCAP_URL: str = os.environ.get("FINVIZ_MIDCAP_URL", "")
FINVIZ_MAJORS_URL: str = os.environ.get("FINVIZ_MAJORS_URL", "")

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; rs-rating/1.0)",
}


def fetch_finviz_tickers(url: str, token: str) -> list[str]:
    """Fetch tickers from a Finviz Elite export URL."""
    import pandas as pd

    resolved = url.replace("{token}", token)
    if "auth=" not in resolved and token:
        sep = "&" if "?" in resolved else "?"
        resolved = f"{resolved}{sep}auth={token}"

    resp = requests.get(resolved, headers=HEADERS, timeout=30)
    resp.raise_for_status()

    if resp.text.strip().startswith("<"):
        raise ValueError(
            "Finviz returned HTML instead of CSV — check your FINVIZ_AUTH_TOKEN.\n"
            f"First 300 chars:\n{resp.text[:300]}"
        )

    df = pd.read_csv(io.StringIO(resp.text))
    ticker_col = next((c for c in df.columns if c.strip().upper() == "TICKER"), None)
    if ticker_col is None:
        raise ValueError(f"No 'Ticker' column found. Columns: {list(df.columns)}")

    return df[ticker_col].dropna().str.strip().tolist()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute an approximate RS score and 1-99 RS rating for a stock."
    )

    source = parser.add_mutually_exclusive_group()
    source.add_argument("ticker", nargs="?", help="Stock ticker, e.g. AAPL")
    source.add_argument(
        "--finviz-url",
        metavar="URL",
        default=FINVIZ_URL or None,
        help="Finviz Elite export URL to scan a universe of stocks. "
             "Falls back to FINVIZ_URL env var. Uses FINVIZ_AUTH_TOKEN for auth.",
    )
    source.add_argument(
        "--daily",
        action="store_true",
        help="Daily outperformance mode: rank stocks by single-day return vs benchmark.",
    )

    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Target date for --daily mode (default: today)",
    )

    parser.add_argument(
        "--benchmark",
        default="SPY",
        help="Benchmark ticker (default: SPY). Use SPY, QQQ, IWM, etc.",
    )
    parser.add_argument(
        "--period",
        default="2y",
        help="Download period for price history (default: 2y)",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="Plot RS line and moving averages (single ticker only)",
    )
    parser.add_argument(
        "--ma1",
        type=int,
        default=21,
        help="First RS moving average length (default: 21)",
    )
    parser.add_argument(
        "--ma2",
        type=int,
        default=50,
        help="Second RS moving average length (default: 50)",
    )
    parser.add_argument(
        "--min-rating",
        type=int,
        default=0,
        metavar="N",
        help="When scanning a universe, only print tickers with RS rating >= N",
    )
    parser.add_argument(
        "--sort",
        choices=["rating", "score", "ticker"],
        default="rating",
        help="Sort order for universe scan results (default: rating)",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=8,
        metavar="N",
        help="Number of parallel download workers in universe mode (default: 8)",
    )
    parser.add_argument(
        "--small-cap",
        action="store_true",
        help="Scan small caps ($300M-$2B) using FINVIZ_SMALLCAP_URL. Saves to results/smallcap/.",
    )
    parser.add_argument(
        "--mid-cap",
        action="store_true",
        help="Scan mid caps ($2B-$10B) using FINVIZ_MIDCAP_URL. Benchmark: MDY. Saves to results/midcap/.",
    )
    parser.add_argument(
        "--majors",
        action="store_true",
        help="Scan large/mega caps ($10B+) using FINVIZ_MAJORS_URL. Benchmark: SPY. Saves to results/majors/.",
    )
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        metavar="N",
        help="Show only the top N ranked stocks in the final table",
    )
    parser.add_argument(
        "--output",
        metavar="FILE",
        default=None,
        help="Save results to a CSV file (default: rs_ratings_YYYY-MM-DD.csv)",
    )
    parser.add_argument(
        "--no-csv",
        action="store_true",
        help="Skip writing a CSV file",
    )
    return parser.parse_args()


def run_single(args: argparse.Namespace) -> None:
    result, df = compute_rs(
        ticker=args.ticker,
        benchmark=args.benchmark,
        period=args.period,
    )

    print(f"Ticker:                {result.ticker}")
    print(f"Benchmark:             {result.benchmark}")
    print(f"Last stock close:      {result.last_close:.2f}")
    print(f"Last benchmark close:  {result.last_benchmark_close:.2f}")
    print(f"Last RS line value:    {result.rs_line_last:.6f}")
    print(f"Weighted RS score:     {result.rs_score:.2f}")
    print(f"Approx RS rating:      {result.rs_rating}")

    if args.plot:
        plot_rs(df, args.ticker, args.ma1, args.ma2)


def run_universe(args: argparse.Namespace) -> None:
    token = FINVIZ_AUTH_TOKEN
    if not token:
        print("ERROR: FINVIZ_AUTH_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    print(f"Fetching tickers from Finviz...")
    tickers = fetch_finviz_tickers(args.finviz_url, token)
    print(f"Found {len(tickers)} tickers. Computing RS ratings...\n")

    results = []
    errors = []
    total = len(tickers)
    completed = 0

    def fetch(ticker: str):
        return ticker, compute_rs(ticker=ticker, benchmark=args.benchmark, period=args.period)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, t): t for t in tickers}
        for future in as_completed(futures):
            completed += 1
            ticker = futures[future]
            try:
                _, (result, _) = future.result()
                results.append(result)
                print(f"  [{completed}/{total}] {ticker:<8} RS rating: {result.rs_rating:>2}  score: {result.rs_score:.2f}")
            except Exception as e:
                errors.append((ticker, str(e)))
                print(f"  [{completed}/{total}] {ticker:<8} ERROR: {e}", file=sys.stderr)

    if not results:
        print("\nNo results to display.")
        return

    if args.min_rating:
        results = [r for r in results if r.rs_rating >= args.min_rating]

    key = {"rating": lambda r: -r.rs_rating, "score": lambda r: -r.rs_score, "ticker": lambda r: r.ticker}[args.sort]
    results.sort(key=key)

    display = results[: args.top] if args.top else results

    width = 58
    print(f"\n{'=' * width}")
    label = f"  TOP {args.top} RS RANKINGS" if args.top else "  RS RANKINGS"
    print(label)
    print(f"{'=' * width}")
    print(f"  {'RANK':<6} {'TICKER':<10} {'RS RATING':>9} {'RS SCORE':>10} {'CLOSE':>8}")
    print(f"  {'-' * (width - 2)}")
    for rank, r in enumerate(display, 1):
        print(f"  {rank:<6} {r.ticker:<10} {r.rs_rating:>9} {r.rs_score:>10.2f} {r.last_close:>8.2f}")
    print(f"{'=' * width}")
    print(f"  {len(display)} stocks shown", end="")
    if args.min_rating:
        print(f" (RS rating >= {args.min_rating})", end="")
    if errors:
        print(f"  |  {len(errors)} error(s)", end="")
    print()

    if not args.no_csv:
        csv_path = args.output or f"rs_ratings_{date.today().isoformat()}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["rank", "ticker", "rs_rating", "rs_score", "close", "benchmark"])
            for rank, r in enumerate(display, 1):
                writer.writerow([rank, r.ticker, r.rs_rating, f"{r.rs_score:.2f}", f"{r.last_close:.2f}", r.benchmark])
        print(f"\n  Saved → {csv_path}")


def run_daily(args: argparse.Namespace) -> None:
    token = FINVIZ_AUTH_TOKEN
    if not token:
        print("ERROR: FINVIZ_AUTH_TOKEN is not set.", file=sys.stderr)
        sys.exit(1)

    finviz_url = args.finviz_url or FINVIZ_URL
    if not finviz_url:
        print("ERROR: provide --finviz-url or set FINVIZ_URL in .env.", file=sys.stderr)
        sys.exit(1)

    target = date.fromisoformat(args.date) if args.date else date.today()

    print(f"Fetching tickers from Finviz...")
    tickers = fetch_finviz_tickers(finviz_url, token)
    print(f"Found {len(tickers)} tickers. Computing daily performance for {target}...\n")

    results = []
    errors = []
    total = len(tickers)
    completed = 0

    def fetch(ticker: str):
        return compute_daily_performance(ticker, target, benchmark=args.benchmark)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(fetch, t): t for t in tickers}
        for future in as_completed(futures):
            completed += 1
            ticker = futures[future]
            try:
                result = future.result()
                results.append(result)
                print(
                    f"  [{completed}/{total}] {ticker:<8} "
                    f"{result.stock_return_pct:+.2f}%  vs benchmark {result.benchmark_return_pct:+.2f}%  "
                    f"rel {result.relative_return_pct:+.2f}%"
                )
            except Exception as e:
                errors.append((ticker, str(e)))
                print(f"  [{completed}/{total}] {ticker:<8} ERROR: {e}", file=sys.stderr)

    if not results:
        print("\nNo results to display.")
        return

    results.sort(key=lambda r: -r.relative_return_pct)
    display = results[: args.top] if args.top else results

    bench = results[0].benchmark_return_pct if results else 0.0
    width = 68
    label = f"  TOP {args.top} DAILY OUTPERFORMERS — {target}" if args.top else f"  DAILY OUTPERFORMERS — {target}"
    print(f"\n{'=' * width}")
    print(label)
    print(f"  Benchmark ({args.benchmark}): {bench:+.2f}%")
    print(f"{'=' * width}")
    print(f"  {'RANK':<6} {'TICKER':<10} {'RETURN':>8} {'VS BENCH':>10} {'CLOSE':>8} {'VOLUME':>12}")
    print(f"  {'-' * (width - 2)}")
    for rank, r in enumerate(display, 1):
        print(
            f"  {rank:<6} {r.ticker:<10} {r.stock_return_pct:>+8.2f}% "
            f"{r.relative_return_pct:>+9.2f}% {r.close:>8.2f} {r.volume:>12,.0f}"
        )
    print(f"{'=' * width}")
    print(f"  {len(display)} stocks shown", end="")
    if errors:
        print(f"  |  {len(errors)} error(s)", end="")
    print()

    if not args.no_csv:
        csv_path = args.output or f"daily_rs_{target}.csv"
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["rank", "ticker", "date", "open", "close", "return_pct", "benchmark_return_pct", "relative_return_pct", "volume", "benchmark"])
            for rank, r in enumerate(display, 1):
                writer.writerow([
                    rank, r.ticker, r.target_date,
                    f"{r.open:.2f}", f"{r.close:.2f}",
                    f"{r.stock_return_pct:.2f}", f"{r.benchmark_return_pct:.2f}",
                    f"{r.relative_return_pct:.2f}", f"{r.volume:.0f}", args.benchmark,
                ])
        print(f"\n  Saved → {csv_path}")


def _resolve_universe(args: argparse.Namespace) -> str:
    """Return the output subdirectory name based on the active universe flag."""
    if args.small_cap:
        return "smallcap"
    if args.mid_cap:
        return "midcap"
    if args.majors:
        return "majors"
    return ""


def main() -> None:
    args = parse_args()

    if args.small_cap:
        if not FINVIZ_SMALLCAP_URL:
            print("ERROR: FINVIZ_SMALLCAP_URL is not set in .env.", file=sys.stderr)
            sys.exit(1)
        args.finviz_url = FINVIZ_SMALLCAP_URL
        if not args.daily and args.benchmark == "SPY":
            args.benchmark = "IWM"

    elif args.mid_cap:
        if not FINVIZ_MIDCAP_URL:
            print("ERROR: FINVIZ_MIDCAP_URL is not set in .env.", file=sys.stderr)
            sys.exit(1)
        args.finviz_url = FINVIZ_MIDCAP_URL
        if not args.daily and args.benchmark == "SPY":
            args.benchmark = "MDY"

    elif args.majors:
        if not FINVIZ_MAJORS_URL:
            print("ERROR: FINVIZ_MAJORS_URL is not set in .env.", file=sys.stderr)
            sys.exit(1)
        args.finviz_url = FINVIZ_MAJORS_URL

    # Set output path into the appropriate results subfolder
    universe_dir = _resolve_universe(args)
    if universe_dir and args.output is None and not getattr(args, "no_csv", False):
        results_dir = os.path.join("results", universe_dir)
        os.makedirs(results_dir, exist_ok=True)
        today = date.today().isoformat()
        if args.daily:
            args.output = os.path.join(results_dir, f"daily_rs_{universe_dir}_{today}.csv")
        else:
            args.output = os.path.join(results_dir, f"rs_ratings_{universe_dir}_{today}.csv")

    if args.ticker:
        run_single(args)
    elif args.daily:
        run_daily(args)
    elif args.finviz_url:
        run_universe(args)
    else:
        print("ERROR: provide a ticker, --daily, or set FINVIZ_URL in .env.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
