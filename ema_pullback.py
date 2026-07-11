#!/usr/bin/env python3
"""
ema_pullback.py — EMA Pullback Screener for Top RS Stocks

Reads the RS ratings CSV for a given date and universe, fetches daily adjusted
closes from Polygon in parallel, computes 8 and 21 EMAs (daily) and 8-week EMA
(weekly), and classifies each stock into tiers.

Usage:
    python ema_pullback.py [--date YYYY-MM-DD] [--universe majors|midcap|smallcap]
                           [--top N] [--workers N] [--output FILE] [--save-insights]

Daily tiers:
    1A  Close within 8 EMA (-1% to +4.5%) AND within 21 EMA (-1% to +4%)
    1B  Close within 8 EMA only
    2A  21 EMA 0–4% above close (not at 8 EMA)
    2B  21 EMA 4–7.5% above close (not at 8 EMA)

Weekly section:
    W8  Close within -2% to +5% of the 8-week EMA

Daily 8 EMA range:
    D8  Close within -2% to +2% of the daily 8 EMA

Daily 21 EMA range:
    D21 Close within -2% to +2% of the daily 21 EMA
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from polygon import RESTClient

load_dotenv(Path(__file__).parent / ".env")

SCRIPT_DIR = Path(__file__).parent.resolve()
RESULTS_DIR = SCRIPT_DIR / "results"
INSIGHTS_DIR = SCRIPT_DIR.parent / "daily_insights"

# EMA proximity bands (% of EMA value)
BAND_8_LOW,   BAND_8_HIGH  = -1.0, 4.5
BAND_21_LOW,  BAND_21_HIGH = -1.0, 4.0
TIGHT_21_LOW, TIGHT_21_HIGH = 0.0, 4.0
NEAR_21_LOW,  NEAR_21_HIGH  = 4.0, 7.5

# 8-week EMA proximity band
W8_EMA_LOW,  W8_EMA_HIGH  = -2.0, 5.0

# Daily 8 EMA narrow range
D8_EMA_LOW,  D8_EMA_HIGH  = -2.0, 2.0

# Daily 21 EMA narrow range
D21_EMA_LOW, D21_EMA_HIGH = -2.0, 2.0

LOOKBACK_DAYS = 180  # calendar days (~26 weeks) — enough for daily EMAs and 8-week EMA
DEFAULT_WORKERS = 8


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EMA pullback screener for top RS stocks.")
    p.add_argument("--date", default=None, metavar="YYYY-MM-DD",
                   help="Date to screen (default: today)")
    p.add_argument("--universe", default="majors",
                   choices=["majors", "midcap", "smallcap"],
                   help="RS universe (default: majors)")
    p.add_argument("--top", type=int, default=None, metavar="N",
                   help="Screen top N RS-ranked stocks (default: all with --min-rs, else 150)")
    p.add_argument("--min-rs", type=int, default=None, metavar="RATING",
                   help="Include all stocks with RS rating >= RATING (overrides --top)")
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS, metavar="N",
                   help=f"Parallel Polygon workers (default: {DEFAULT_WORKERS})")
    p.add_argument("--output", metavar="FILE", default=None,
                   help="Write results to a CSV file")
    p.add_argument("--save-insights", action="store_true",
                   help="Save a markdown report to daily_insights/")
    p.add_argument("--no-print", action="store_true",
                   help="Suppress console output")
    return p.parse_args()


# ── Data loading ─────────────────────────────────────────────────────────────

def load_top_rs(universe: str, target_date: date, top_n: int | None,
                min_rs: int | None) -> list[dict]:
    path = RESULTS_DIR / universe / f"rs_ratings_{universe}_{target_date}.csv"
    if not path.exists():
        print(f"ERROR: {path} not found.", file=sys.stderr)
        sys.exit(1)

    rows = []
    with open(path) as f:
        for row in csv.DictReader(f):
            rs_score  = float(row["rs_score"])
            rs_rating = int(row["rs_rating"])
            rank      = int(row["rank"])
            if rs_score > 1_000_000:
                continue
            if min_rs is not None:
                if rs_rating < min_rs:
                    continue
            elif top_n is not None and rank > top_n:
                continue
            rows.append({
                "ticker":    row["ticker"],
                "rank":      rank,
                "rs_rating": rs_rating,
                "rs_score":  rs_score,
            })
    rows.sort(key=lambda r: r["rank"])
    return rows


# ── Polygon fetching ─────────────────────────────────────────────────────────

def _fetch_one(ticker: str, start: str, end: str, api_key: str) -> tuple[str, dict | None]:
    """Fetch closes for one ticker and compute EMAs. Returns (ticker, result|None)."""
    try:
        client = RESTClient(api_key=api_key)
        aggs = client.get_aggs(
            ticker, 1, "day", start, end,
            adjusted=True, sort="asc", limit=5000,
        )
        if not aggs:
            return ticker, None

        closes = pd.Series(
            {pd.Timestamp(a.timestamp, unit="ms", tz="UTC")
               .tz_convert(None).normalize(): a.close
             for a in aggs if a.close is not None},
            name=ticker,
        ).sort_index()

        if len(closes) < 21:
            return ticker, None

        ema8  = float(closes.ewm(span=8,  adjust=False).mean().iloc[-1])
        ema21 = float(closes.ewm(span=21, adjust=False).mean().iloc[-1])
        close = float(closes.iloc[-1])

        # 8-week EMA: resample daily closes to weekly (last close of each week)
        weekly = closes.resample("W").last().dropna()
        if len(weekly) >= 8:
            ema_w8  = float(weekly.ewm(span=8, adjust=False).mean().iloc[-1])
            pct_w8  = (close - ema_w8) / ema_w8 * 100
        else:
            ema_w8 = None
            pct_w8 = None

        return ticker, {
            "close":  close,
            "pct8":   (close - ema8)  / ema8  * 100,
            "pct21":  (close - ema21) / ema21 * 100,
            "pct_w8": pct_w8,
        }
    except Exception as e:
        return ticker, None


def fetch_emas(tickers: list[str], api_key: str, workers: int,
               target_date: date) -> dict[str, dict]:
    end   = target_date.isoformat()
    start = (target_date - timedelta(days=LOOKBACK_DAYS)).isoformat()

    results: dict[str, dict] = {}
    errors: list[str] = []
    total = len(tickers)

    print(f"  Fetching {total} tickers from Polygon ({workers} workers)...")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_fetch_one, t, start, end, api_key): t for t in tickers}
        for i, future in enumerate(as_completed(futures), 1):
            ticker, data = future.result()
            if data:
                results[ticker] = data
            else:
                errors.append(ticker)
            if i % 25 == 0 or i == total:
                print(f"    {i}/{total} done, {len(results)} with data", end="\r")

    print()
    if errors:
        print(f"  No data for {len(errors)} tickers: {', '.join(sorted(errors))}", file=sys.stderr)

    return results


# ── Tier classification ───────────────────────────────────────────────────────

def classify(pct8: float | None, pct21: float | None) -> str:
    at_8    = pct8  is not None and BAND_8_LOW   <= pct8  <= BAND_8_HIGH
    at_21   = pct21 is not None and BAND_21_LOW  <= pct21 <= BAND_21_HIGH
    tight21 = pct21 is not None and TIGHT_21_LOW <  pct21 <= TIGHT_21_HIGH
    near21  = pct21 is not None and NEAR_21_LOW  <  pct21 <= NEAR_21_HIGH

    if at_8 and at_21: return "1A"
    if at_8:           return "1B"
    if tight21:        return "2A"
    if near21:         return "2B"
    return ""


def build_tiers(top_rs: list[dict], ema_map: dict) -> dict[str, list[dict]]:
    tiers: dict[str, list[dict]] = {"1A": [], "1B": [], "2A": [], "2B": []}
    for row in top_rs:
        t = row["ticker"]
        if t not in ema_map:
            continue
        ed = ema_map[t]
        tier = classify(ed.get("pct8"), ed.get("pct21"))
        if not tier:
            continue
        tiers[tier].append({
            "ticker":    t,
            "rank":      row["rank"],
            "rs_rating": row["rs_rating"],
            "close":     round(ed["close"], 2),
            "pct8":      round(ed["pct8"],  2) if ed.get("pct8")  is not None else None,
            "pct21":     round(ed["pct21"], 2) if ed.get("pct21") is not None else None,
        })
    for rows in tiers.values():
        rows.sort(key=lambda r: r["rank"])
    return tiers


def build_weekly8_section(top_rs: list[dict], ema_map: dict) -> list[dict]:
    """Return stocks whose close sits within the 8-week EMA proximity band."""
    results = []
    for row in top_rs:
        t = row["ticker"]
        if t not in ema_map:
            continue
        pct_w8 = ema_map[t].get("pct_w8")
        if pct_w8 is None:
            continue
        if W8_EMA_LOW <= pct_w8 <= W8_EMA_HIGH:
            results.append({
                "ticker":    t,
                "rank":      row["rank"],
                "rs_rating": row["rs_rating"],
                "close":     round(ema_map[t]["close"], 2),
                "pct_w8":    round(pct_w8, 2),
                "pct8":      round(ema_map[t]["pct8"], 2) if ema_map[t].get("pct8") is not None else None,
                "pct21":     round(ema_map[t]["pct21"], 2) if ema_map[t].get("pct21") is not None else None,
            })
    results.sort(key=lambda r: r["rank"])
    return results


def build_d8_section(top_rs: list[dict], ema_map: dict) -> list[dict]:
    """Return stocks whose close sits within -2% to +2% of the daily 8 EMA."""
    results = []
    for row in top_rs:
        t = row["ticker"]
        if t not in ema_map:
            continue
        pct8 = ema_map[t].get("pct8")
        if pct8 is None:
            continue
        if D8_EMA_LOW <= pct8 <= D8_EMA_HIGH:
            results.append({
                "ticker":    t,
                "rank":      row["rank"],
                "rs_rating": row["rs_rating"],
                "close":     round(ema_map[t]["close"], 2),
                "pct8":      round(pct8, 2),
                "pct21":     round(ema_map[t]["pct21"], 2) if ema_map[t].get("pct21") is not None else None,
            })
    results.sort(key=lambda r: r["rank"])
    return results


def build_d21_section(top_rs: list[dict], ema_map: dict) -> list[dict]:
    """Return stocks whose close sits within -2% to +2% of the daily 21 EMA."""
    results = []
    for row in top_rs:
        t = row["ticker"]
        if t not in ema_map:
            continue
        pct21 = ema_map[t].get("pct21")
        if pct21 is None:
            continue
        if D21_EMA_LOW <= pct21 <= D21_EMA_HIGH:
            results.append({
                "ticker":    t,
                "rank":      row["rank"],
                "rs_rating": row["rs_rating"],
                "close":     round(ema_map[t]["close"], 2),
                "pct8":      round(ema_map[t]["pct8"], 2) if ema_map[t].get("pct8") is not None else None,
                "pct21":     round(pct21, 2),
            })
    results.sort(key=lambda r: r["rank"])
    return results


# ── Formatting ───────────────────────────────────────────────────────────────

def fmt_pct(v: float | None) -> str:
    if v is None:
        return "   n/a"
    return f"{'+' if v >= 0 else ''}{v:.2f}%"


def print_tier(label: str, desc: str, rows: list[dict]) -> None:
    if not rows:
        return
    print(f"\n{'─' * 72}")
    print(f"  TIER {label} — {desc} ({len(rows)} names)")
    print(f"{'─' * 72}")
    print(f"  {'TICKER':<8} {'RANK':>5} {'RS':>4} {'CLOSE':>8}  {'8 EMA':>8}  {'21 EMA':>8}")
    print(f"  {'─' * 62}")
    for r in rows:
        print(
            f"  {r['ticker']:<8} {r['rank']:>5} {r['rs_rating']:>4} "
            f"${r['close']:>7.2f}  {fmt_pct(r['pct8']):>8}  {fmt_pct(r['pct21']):>8}"
        )


def print_weekly8(rows: list[dict]) -> None:
    if not rows:
        return
    print(f"\n{'─' * 72}")
    print(f"  8-WEEK EMA PULLBACK — {len(rows)} names (close within −2% to +5% of 8wk EMA)")
    print(f"{'─' * 72}")
    print(f"  {'TICKER':<8} {'RANK':>5} {'RS':>4} {'CLOSE':>8}  {'vs 8wk EMA':>12}  {'vs 8d EMA':>10}  {'vs 21d EMA':>10}")
    print(f"  {'─' * 66}")
    for r in rows:
        print(
            f"  {r['ticker']:<8} {r['rank']:>5} {r['rs_rating']:>4} "
            f"${r['close']:>7.2f}  {fmt_pct(r['pct_w8']):>12}  "
            f"{fmt_pct(r['pct8']):>10}  {fmt_pct(r['pct21']):>10}"
        )


def print_d8(rows: list[dict]) -> None:
    if not rows:
        return
    print(f"\n{'─' * 72}")
    print(f"  8 EMA RANGE — {len(rows)} names (close within −2% to +2% of daily 8 EMA)")
    print(f"{'─' * 72}")
    print(f"  {'TICKER':<8} {'RANK':>5} {'RS':>4} {'CLOSE':>8}  {'vs 8d EMA':>10}  {'vs 21d EMA':>10}")
    print(f"  {'─' * 62}")
    for r in rows:
        print(
            f"  {r['ticker']:<8} {r['rank']:>5} {r['rs_rating']:>4} "
            f"${r['close']:>7.2f}  {fmt_pct(r['pct8']):>10}  {fmt_pct(r['pct21']):>10}"
        )


def print_d21(rows: list[dict]) -> None:
    if not rows:
        return
    print(f"\n{'─' * 72}")
    print(f"  21 EMA RANGE — {len(rows)} names (close within −2% to +2% of daily 21 EMA)")
    print(f"{'─' * 72}")
    print(f"  {'TICKER':<8} {'RANK':>5} {'RS':>4} {'CLOSE':>8}  {'vs 8d EMA':>10}  {'vs 21d EMA':>10}")
    print(f"  {'─' * 62}")
    for r in rows:
        print(
            f"  {r['ticker']:<8} {r['rank']:>5} {r['rs_rating']:>4} "
            f"${r['close']:>7.2f}  {fmt_pct(r['pct8']):>10}  {fmt_pct(r['pct21']):>10}"
        )


def print_report(tiers: dict, weekly8: list[dict], d8: list[dict], d21: list[dict],
                 target_date: date, universe: str, filter_label: str) -> None:
    total = sum(len(v) for v in tiers.values())
    print(f"\n{'═' * 72}")
    print(f"  EMA PULLBACK SCREENER — {target_date} — {universe.upper()} {filter_label}")
    print(f"  {total} qualifying names across 4 daily tiers  |  {len(weekly8)} at 8-week EMA  |  "
          f"{len(d8)} in 8 EMA range  |  {len(d21)} in 21 EMA range")
    print(f"{'═' * 72}")
    print_tier("1A", "At both 8 EMA and 21 EMA", tiers["1A"])
    print_tier("1B", "At 8 EMA only (21 EMA further below)", tiers["1B"])
    print_tier("2A", "Tight 21 EMA (0–4% above close)", tiers["2A"])
    print_tier("2B", "Near 21 EMA (4–7.5% above close)", tiers["2B"])
    print_weekly8(weekly8)
    print_d8(d8)
    print_d21(d21)
    print()


# ── Markdown output ───────────────────────────────────────────────────────────

def tier_table(rows: list[dict]) -> str:
    if not rows:
        return "_None_\n"
    lines = [
        "| Ticker | RS Rank | RS Rating | Close | % vs 8 EMA | % vs 21 EMA |",
        "|--------|---------|-----------|-------|-----------|------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {r['rank']} | {r['rs_rating']} "
            f"| ${r['close']:.2f} | {fmt_pct(r['pct8']).strip()} | {fmt_pct(r['pct21']).strip()} |"
        )
    return "\n".join(lines) + "\n"


def weekly8_table(rows: list[dict]) -> str:
    if not rows:
        return "_None_\n"
    lines = [
        "| Ticker | RS Rank | RS Rating | Close | % vs 8wk EMA | % vs 8d EMA | % vs 21d EMA |",
        "|--------|---------|-----------|-------|-------------|------------|-------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {r['rank']} | {r['rs_rating']} "
            f"| ${r['close']:.2f} | {fmt_pct(r['pct_w8']).strip()} "
            f"| {fmt_pct(r['pct8']).strip()} | {fmt_pct(r['pct21']).strip()} |"
        )
    return "\n".join(lines) + "\n"


def d8_table(rows: list[dict]) -> str:
    if not rows:
        return "_None_\n"
    lines = [
        "| Ticker | RS Rank | RS Rating | Close | % vs 8 EMA | % vs 21 EMA |",
        "|--------|---------|-----------|-------|-----------|------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {r['rank']} | {r['rs_rating']} "
            f"| ${r['close']:.2f} | {fmt_pct(r['pct8']).strip()} | {fmt_pct(r['pct21']).strip()} |"
        )
    return "\n".join(lines) + "\n"


def d21_table(rows: list[dict]) -> str:
    if not rows:
        return "_None_\n"
    lines = [
        "| Ticker | RS Rank | RS Rating | Close | % vs 8 EMA | % vs 21 EMA |",
        "|--------|---------|-----------|-------|-----------|------------|",
    ]
    for r in rows:
        lines.append(
            f"| {r['ticker']} | {r['rank']} | {r['rs_rating']} "
            f"| ${r['close']:.2f} | {fmt_pct(r['pct8']).strip()} | {fmt_pct(r['pct21']).strip()} |"
        )
    return "\n".join(lines) + "\n"


def build_markdown(tiers: dict, weekly8: list[dict], d8: list[dict], d21: list[dict],
                   target_date: date, universe: str, filter_label: str) -> str:
    total = sum(len(v) for v in tiers.values())
    return "\n".join([
        f"# RS {universe.capitalize()} {filter_label} — EMA Pullback Analysis — {target_date}",
        "",
        f"**Universe:** {universe.capitalize()} stocks with {filter_label}  ",
        f"**Source:** `rs_ratings_{universe}_{target_date}.csv` via Polygon daily closes  ",
        f"**Date:** {target_date}  ",
        f"**Total qualifying (daily tiers):** {total} names across 4 tiers  ",
        f"**At 8-week EMA:** {len(weekly8)} names  ",
        f"**In 8 EMA range (−2% to +2%):** {len(d8)} names  ",
        f"**In 21 EMA range (−2% to +2%):** {len(d21)} names",
        "",
        "---",
        "",
        "## Tier Definitions",
        "",
        "| Tier | Criteria | Interpretation |",
        "|------|----------|----------------|",
        "| **1A** | Within 8 EMA (−1% to +4.5%) **AND** within 21 EMA (−1% to +4%) | Riding both EMAs — tightest pullback |",
        "| **1B** | Within 8 EMA only (−1% to +4.5%) | At 8 EMA; 21 EMA further below |",
        "| **2A** | 21 EMA 0–4% above close | Approaching 21 EMA from below |",
        "| **2B** | 21 EMA 4–7.5% above close | Near 21 EMA — needs more cooling |",
        "| **W8** | Within −2% to +5% of 8-week EMA | Weekly timeframe pullback to 8wk EMA |",
        "| **D8** | Within −2% to +2% of daily 8 EMA | Tight daily range around the 8 EMA |",
        "| **D21** | Within −2% to +2% of daily 21 EMA | Tight daily range around the 21 EMA |",
        "",
        "---",
        "",
        f"## Tier 1A — At Both 8 EMA and 21 EMA ({len(tiers['1A'])} names)",
        "",
        tier_table(tiers["1A"]),
        "",
        f"## Tier 1B — At 8 EMA Only ({len(tiers['1B'])} names)",
        "",
        tier_table(tiers["1B"]),
        "",
        f"## Tier 2A — Tight 21 EMA, 0–4% Above Close ({len(tiers['2A'])} names)",
        "",
        tier_table(tiers["2A"]),
        "",
        f"## Tier 2B — Near 21 EMA, 4–7.5% Above Close ({len(tiers['2B'])} names)",
        "",
        tier_table(tiers["2B"]),
        "",
        f"## 8-Week EMA Pullback — Close Within −2% to +5% of 8wk EMA ({len(weekly8)} names)",
        "",
        weekly8_table(weekly8),
        "",
        f"## 8 EMA Range — Close Within −2% to +2% of Daily 8 EMA ({len(d8)} names)",
        "",
        d8_table(d8),
        "",
        f"## 21 EMA Range — Close Within −2% to +2% of Daily 21 EMA ({len(d21)} names)",
        "",
        d21_table(d21),
        "",
    ])


# ── CSV output ────────────────────────────────────────────────────────────────

def save_csv(tiers: dict, weekly8: list[dict], d8: list[dict], d21: list[dict], path: str) -> None:
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["tier", "ticker", "rank", "rs_rating", "close",
                    "pct_from_8ema", "pct_from_21ema", "pct_from_8wk_ema"])
        for tier_name, rows in tiers.items():
            for r in rows:
                w.writerow([
                    tier_name, r["ticker"], r["rank"], r["rs_rating"],
                    f"{r['close']:.2f}",
                    f"{r['pct8']:.2f}"  if r["pct8"]  is not None else "",
                    f"{r['pct21']:.2f}" if r["pct21"] is not None else "",
                    "",
                ])
        for r in weekly8:
            w.writerow([
                "W8", r["ticker"], r["rank"], r["rs_rating"],
                f"{r['close']:.2f}",
                f"{r['pct8']:.2f}"   if r["pct8"]   is not None else "",
                f"{r['pct21']:.2f}"  if r["pct21"]  is not None else "",
                f"{r['pct_w8']:.2f}" if r["pct_w8"] is not None else "",
            ])
        for r in d8:
            w.writerow([
                "D8", r["ticker"], r["rank"], r["rs_rating"],
                f"{r['close']:.2f}",
                f"{r['pct8']:.2f}"  if r["pct8"]  is not None else "",
                f"{r['pct21']:.2f}" if r["pct21"] is not None else "",
                "",
            ])
        for r in d21:
            w.writerow([
                "D21", r["ticker"], r["rank"], r["rs_rating"],
                f"{r['close']:.2f}",
                f"{r['pct8']:.2f}"  if r["pct8"]  is not None else "",
                f"{r['pct21']:.2f}" if r["pct21"] is not None else "",
                "",
            ])
    print(f"  CSV saved → {path}")


def save_markdown(content: str, target_date: date, universe: str,
                  min_rs: int | None = None) -> None:
    out_dir = INSIGHTS_DIR / str(target_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_rs{min_rs}plus" if min_rs else ""
    path = out_dir / f"{target_date}_rs_ema_pullbacks_{universe}{suffix}.md"
    path.write_text(content)
    print(f"  Markdown saved → {path}")


def save_tickers_txt(tiers: dict, weekly8: list[dict], d8: list[dict], d21: list[dict],
                     target_date: date, universe: str, min_rs: int | None = None) -> None:
    out_dir = INSIGHTS_DIR / str(target_date)
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = f"_rs{min_rs}plus" if min_rs else ""
    path = out_dir / f"{target_date}_rs_ema_pullbacks_{universe}{suffix}.txt"
    tier_labels = {
        "1A": "Tier 1A — At both 8 EMA and 21 EMA",
        "1B": "Tier 1B — At 8 EMA only",
        "2A": "Tier 2A — Tight 21 EMA (0–4% above close)",
        "2B": "Tier 2B — Near 21 EMA (4–7.5% above close)",
    }
    lines = []
    for tier, rows in tiers.items():
        lines.append(f"[{tier_labels[tier]}]")
        lines.append(", ".join(r["ticker"] for r in rows) if rows else "(none)")
        lines.append("")
    lines.append("[8-Week EMA Pullback — Close within −2% to +5% of 8wk EMA]")
    lines.append(", ".join(r["ticker"] for r in weekly8) if weekly8 else "(none)")
    lines.append("")
    lines.append("[8 EMA Range — Close within −2% to +2% of daily 8 EMA]")
    lines.append(", ".join(r["ticker"] for r in d8) if d8 else "(none)")
    lines.append("")
    lines.append("[21 EMA Range — Close within −2% to +2% of daily 21 EMA]")
    lines.append(", ".join(r["ticker"] for r in d21) if d21 else "(none)")
    lines.append("")
    path.write_text("\n".join(lines).strip() + "\n")
    print(f"  Tickers saved → {path}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    args = parse_args()
    target_date = date.fromisoformat(args.date) if args.date else date.today()

    api_key = os.environ.get("POLYGON_API_KEY", "")
    if not api_key:
        print("ERROR: POLYGON_API_KEY not set in .env or environment.", file=sys.stderr)
        sys.exit(1)

    # Resolve effective filter for display
    top_n  = args.top if args.min_rs is None else None
    if top_n is None and args.min_rs is None:
        top_n = 150

    label = f"RS rating >= {args.min_rs}" if args.min_rs else f"top {top_n}"
    print(f"Loading {args.universe} ({label}) for {target_date}...")
    top_rs  = load_top_rs(args.universe, target_date, top_n, args.min_rs)
    tickers = [r["ticker"] for r in top_rs]
    print(f"  {len(tickers)} tickers loaded.")

    ema_map = fetch_emas(tickers, api_key, args.workers, target_date)
    print(f"  EMA data collected for {len(ema_map)}/{len(tickers)} tickers.")

    tiers   = build_tiers(top_rs, ema_map)
    weekly8 = build_weekly8_section(top_rs, ema_map)
    d8      = build_d8_section(top_rs, ema_map)
    d21     = build_d21_section(top_rs, ema_map)

    filter_label = f"RS>={args.min_rs}" if args.min_rs else f"top{top_n}"

    if not args.no_print:
        print_report(tiers, weekly8, d8, d21, target_date, args.universe, filter_label)

    if args.output:
        save_csv(tiers, weekly8, d8, d21, args.output)

    if args.save_insights:
        md = build_markdown(tiers, weekly8, d8, d21, target_date, args.universe, filter_label)
        save_markdown(md, target_date, args.universe, args.min_rs)
        save_tickers_txt(tiers, weekly8, d8, d21, target_date, args.universe, args.min_rs)


if __name__ == "__main__":
    main()
