"""
Phase SEB-X v2-VIZ Task 3 -- PnL distribution charts.

Per stack:
  pnl_hist_<stack>.png     -- histogram of realized_ret_pct with EV/median/zero lines
  pnl_by_year_<stack>.png  -- box plot by year
  pnl_by_bucket_<stack>.png -- box plot by session_bucket
equity_curve.png            -- cumulative realized PnL (all stacks + 2024 segment)

Outputs: results/seb_x_v2viz/*.png
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = _REPO_ROOT / "results" / "seb_x_v2viz"
IN_PARQUET = OUTPUT_DIR / "per_trade_exits.parquet"

_STACK_COLORS = {
    "B0":              "#666666",
    "B0+R1+R3_vwap":  "#1f77b4",
    "B0+R1+R3_prim":  "#ff7f0e",
}


def _sanitize(label: str) -> str:
    return label.replace("+", "_").replace(" ", "_")


def plot_histograms(df: pd.DataFrame) -> None:
    for stack in df["stack"].unique():
        sub  = df[df["stack"] == stack]
        data = sub["realized_ret_pct"].dropna().values * 100  # in percent

        ev     = np.mean(data)
        median = np.median(data)
        cvar5  = np.percentile(data, 5)

        fig, ax = plt.subplots(figsize=(10, 6))

        n_bins = min(80, max(20, len(data) // 10))
        n_plot, bins, patches = ax.hist(data, bins=n_bins, color=_STACK_COLORS.get(stack, "#4a90e2"),
                                        alpha=0.7, edgecolor="none", label="trades")

        # Shade left tail (worse than CVaR5 cutoff)
        for patch, left in zip(patches, bins[:-1]):
            if left <= cvar5:
                patch.set_facecolor("#d62728")
                patch.set_alpha(0.8)

        ax.axvline(0,      color="black",  linewidth=1.2, linestyle="-",  label="zero")
        ax.axvline(ev,     color="#2ca02c", linewidth=2.0, linestyle="--", label=f"EV ({ev:.2f}%)")
        ax.axvline(median, color="#ff7f0e", linewidth=2.0, linestyle="-.", label=f"Median ({median:.2f}%)")
        ax.axvline(cvar5,  color="#d62728", linewidth=1.5, linestyle=":",  label=f"CVaR5 ({cvar5:.2f}%)")

        ax.set_xlabel("Realized Return (%)", fontsize=12)
        ax.set_ylabel("Trade Count", fontsize=12)
        ax.set_title(f"PnL Distribution — {stack}\n"
                     f"n={len(data)}  EV={ev:.2f}%  median={median:.2f}%  "
                     f"CVaR5={cvar5:.2f}%  %win={100*(data>0).mean():.1f}%", fontsize=11)
        ax.legend(fontsize=10)
        ax.grid(axis="y", alpha=0.3)

        out = OUTPUT_DIR / f"pnl_hist_{_sanitize(stack)}.png"
        fig.tight_layout()
        fig.savefig(str(out), dpi=130)
        plt.close(fig)
        log.info("Wrote %s", out)


def _boxplot_group(ax: plt.Axes, groups: list[str], data_list: list[np.ndarray],
                   title: str, xlabel: str, color: str) -> None:
    bp = ax.boxplot(
        data_list,
        labels=groups,
        patch_artist=True,
        showfliers=False,
        medianprops=dict(color="black", linewidth=2),
        boxprops=dict(facecolor=color, alpha=0.6),
        whiskerprops=dict(linewidth=1.2),
        capprops=dict(linewidth=1.2),
    )
    ax.axhline(0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
    ax.set_xlabel(xlabel, fontsize=11)
    ax.set_ylabel("Realized Return (%)", fontsize=11)
    ax.set_title(title, fontsize=11)
    ax.yaxis.set_major_formatter(mticker.PercentFormatter(xmax=1.0, decimals=0))
    ax.grid(axis="y", alpha=0.3)


def plot_by_year(df: pd.DataFrame) -> None:
    years = sorted(df["year"].unique())
    for stack in df["stack"].unique():
        sub    = df[df["stack"] == stack]
        groups = [str(y) for y in years if y in sub["year"].values]
        data   = [sub[sub["year"] == int(g)]["realized_ret_pct"].dropna().values for g in groups]

        fig, ax = plt.subplots(figsize=(10, 6))
        _boxplot_group(ax, groups, data,
                       title=f"PnL by Year — {stack}",
                       xlabel="Year",
                       color=_STACK_COLORS.get(stack, "#4a90e2"))

        # Overlay median dots
        for i, (g, d) in enumerate(zip(groups, data), start=1):
            if len(d) > 0:
                ax.plot(i, np.median(d), "o", color="red", markersize=6, zorder=5)

        out = OUTPUT_DIR / f"pnl_by_year_{_sanitize(stack)}.png"
        fig.tight_layout()
        fig.savefig(str(out), dpi=130)
        plt.close(fig)
        log.info("Wrote %s", out)


def plot_by_bucket(df: pd.DataFrame) -> None:
    bucket_order = ["regular_hours", "pre_market", "post_market"]
    for stack in df["stack"].unique():
        sub = df[df["stack"] == stack]
        groups = [b for b in bucket_order if b in sub["session_bucket"].values]
        data   = [sub[sub["session_bucket"] == b]["realized_ret_pct"].dropna().values
                  for b in groups]

        fig, ax = plt.subplots(figsize=(8, 6))
        _boxplot_group(ax, groups, data,
                       title=f"PnL by Session Bucket — {stack}",
                       xlabel="Session Bucket",
                       color=_STACK_COLORS.get(stack, "#4a90e2"))

        for i, d in enumerate(data, start=1):
            if len(d) > 0:
                ax.plot(i, np.median(d), "o", color="red", markersize=6, zorder=5)

        out = OUTPUT_DIR / f"pnl_by_bucket_{_sanitize(stack)}.png"
        fig.tight_layout()
        fig.savefig(str(out), dpi=130)
        plt.close(fig)
        log.info("Wrote %s", out)


def plot_equity_curve(df: pd.DataFrame) -> None:
    """Cumulative realized PnL over time (ordered by date) for all stacks."""
    fig, ax = plt.subplots(figsize=(14, 7))

    for stack in df["stack"].unique():
        sub  = df[df["stack"] == stack].sort_values(["date", "entry_ts_ns"])
        pnl  = sub["realized_ret_pct"].values
        cum  = np.cumsum(pnl)
        color = _STACK_COLORS.get(stack, "#999999")
        ax.plot(range(len(cum)), cum * 100, label=stack, color=color, linewidth=1.5)

        # Mark where 2024 starts
        idx_2024 = sub[sub["year"] >= 2024].index
        if len(idx_2024) > 0:
            pos = sub.index.get_loc(idx_2024[0]) if not isinstance(
                sub.index.get_loc(idx_2024[0]), int) else sub.index.get_loc(idx_2024[0])
            # Find the position in the sorted sequence
            first_2024_pos = (sub["year"] < 2024).sum()
            ax.axvline(first_2024_pos, color=color, linewidth=1.0, linestyle=":",
                       alpha=0.6)

    # Add a single 2024-start label from the first stack
    first_stack = list(df["stack"].unique())[0]
    first_sub   = df[df["stack"] == first_stack].sort_values(["date", "entry_ts_ns"])
    first_2024  = (first_sub["year"] < 2024).sum()
    ax.axvline(first_2024, color="black", linewidth=1.0, linestyle="--", alpha=0.4,
               label="2024 start")

    ax.axhline(0, color="black", linewidth=0.8, linestyle="-", alpha=0.3)
    ax.set_xlabel("Trade sequence (sorted by date)", fontsize=12)
    ax.set_ylabel("Cumulative realized return (sum of % returns)", fontsize=12)
    ax.set_title("Equity Curve — Cumulative Realized Returns\n"
                 "B0+R1+R3_vwap vs B0+R1+R3_prim vs B0 baseline", fontsize=12)
    ax.legend(fontsize=11)
    ax.grid(alpha=0.25)

    out = OUTPUT_DIR / "equity_curve.png"
    fig.tight_layout()
    fig.savefig(str(out), dpi=130)
    plt.close(fig)
    log.info("Wrote %s", out)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    df = pd.read_parquet(str(IN_PARQUET))
    log.info("Loaded %d rows from per_trade_exits.parquet", len(df))

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    plot_histograms(df)
    plot_by_year(df)
    plot_by_bucket(df)
    plot_equity_curve(df)

    log.info("Task 3 complete.")


if __name__ == "__main__":
    main()
