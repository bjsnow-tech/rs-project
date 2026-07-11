from __future__ import annotations

import matplotlib.pyplot as plt
import pandas as pd


def plot_rs(df: pd.DataFrame, ticker: str, ma1: int = 21, ma2: int = 50) -> None:
    """Plot the RS line with two EMA overlays."""
    plot_df = df.copy()
    plot_df["rs_ma1"] = plot_df["rs_line"].ewm(span=ma1, adjust=False).mean()
    plot_df["rs_ma2"] = plot_df["rs_line"].ewm(span=ma2, adjust=False).mean()

    plt.figure(figsize=(12, 6))
    plt.plot(plot_df.index, plot_df["rs_line"], label="RS Line")
    plt.plot(plot_df.index, plot_df["rs_ma1"], label=f"EMA {ma1}")
    plt.plot(plot_df.index, plot_df["rs_ma2"], label=f"EMA {ma2}")
    plt.title(f"{ticker} Relative Strength Line")
    plt.xlabel("Date")
    plt.ylabel("Stock / Benchmark")
    plt.legend()
    plt.tight_layout()
    plt.show()
