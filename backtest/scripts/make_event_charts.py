"""
Generate 1-minute candlestick chart PNGs for all 100 MDR>=150 diagnostic events.

Shows ALL data in each event's parquet (full multi-day span, extended hours).
Dark mode. Scanner hit + event-date RTH open marked. Day boundaries marked.

Output: backtest/results/event_charts_mdr150/{dir_name}.png

Usage:
    python backtest/scripts/make_event_charts.py
    python backtest/scripts/make_event_charts.py --overwrite
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import numpy as np
import pandas as pd
import pyarrow.parquet as pq

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))

from data.schemas.mom_db import FILTERED_DIR  # noqa: E402

EVENT_FILE = Path(r"D:\Trading Research\data\val_mdr150_diagnostic.json")
OUT_DIR    = PROJECT_ROOT / "backtest" / "results" / "event_charts_mdr150"
ET         = "America/New_York"

# ── Style ──────────────────────────────────────────────────────────────────
_MC = mpf.make_marketcolors(
    up="#26a69a",  down="#ef5350",
    edge="inherit",
    wick={"up": "#26a69a", "down": "#ef5350"},
    volume={"up": "#1a7a72", "down": "#a63c38"},
)
_STYLE = mpf.make_mpf_style(
    base_mpf_style="nightclouds",
    marketcolors=_MC,
    gridstyle=":",
    gridcolor="#1e1e1e",
    facecolor="#111111",
    edgecolor="#2a2a2a",
    figcolor="#0a0a0a",
    rc={
        "axes.labelcolor":  "#aaaaaa",
        "xtick.color":      "#777777",
        "ytick.color":      "#777777",
        "xtick.labelsize":  7,
        "ytick.labelsize":  8,
        "axes.titlecolor":  "#eeeeee",
        "axes.titlesize":   11,
        "axes.titlepad":    8,
    },
)


def _load_all_bars(dir_name: str) -> pd.DataFrame:
    """Load every trade in the parquet, build 1-min OHLCV, return ET-indexed DataFrame."""
    path = FILTERED_DIR / dir_name / "trades.parquet"
    if not path.exists():
        return pd.DataFrame()

    tbl    = pq.read_table(str(path), columns=["sip_timestamp", "price", "size"])
    ts     = tbl.column("sip_timestamp").to_numpy()
    prices = tbl.column("price").to_numpy().astype(np.float64)
    sizes  = tbl.column("size").to_numpy().astype(np.int64)

    order  = np.argsort(ts)
    ts     = ts[order];  prices = prices[order];  sizes = sizes[order]

    df = pd.DataFrame({
        "ts":    pd.to_datetime(ts, unit="ns", utc=True).tz_convert(ET),
        "price": prices,
        "size":  sizes,
    }).set_index("ts")

    bars = df.resample("1min").agg(
        Open=("price", "first"),
        High=("price", "max"),
        Low=("price", "min"),
        Close=("price", "last"),
        Volume=("size", "sum"),
    )
    return bars.dropna(subset=["Open"])


def _make_chart(ev: dict, out_path: Path) -> str:
    """Build and save one chart. Returns 'ok', 'skip:<reason>', or 'err:<msg>'."""
    ticker   = ev["ticker"]
    date     = ev["date"]
    mom_pct  = ev["mom_pct"]
    dir_name = ev["dir_name"]

    bars = _load_all_bars(dir_name)
    if bars.empty or len(bars) < 5:
        return f"skip:only {len(bars)} bars"

    bars.index.name = "Date"
    n_days = bars.index.normalize().nunique()
    n_bars = len(bars)

    # ── Vertical lines ──────────────────────────────────────────────────
    vline_times  = []
    vline_colors = []
    vline_widths = []
    vline_alpha  = []

    # Day boundaries: 4am ET start of each trading day (subtle gray)
    for day in pd.to_datetime(bars.index.date).unique():
        boundary = pd.Timestamp(day).tz_localize(ET).replace(hour=4, minute=0)
        if bars.index.min() < boundary < bars.index.max():
            vline_times.append(boundary)
            vline_colors.append("#333333")
            vline_widths.append(0.8)
            vline_alpha.append(0.9)

    # RTH open on event date (cyan)
    rth_open = pd.Timestamp(f"{date} 09:30:00", tz=ET)
    if bars.index.min() <= rth_open <= bars.index.max():
        vline_times.append(rth_open)
        vline_colors.append("#40c4ff")
        vline_widths.append(1.5)
        vline_alpha.append(0.9)

    # Scanner hit (orange, thickest)
    hit_ns = ev.get("scanner_hit_ts_ns")
    if hit_ns:
        hit_ts = pd.Timestamp(hit_ns, unit="ns", tz="UTC").tz_convert(ET)
        vline_times.append(hit_ts)
        vline_colors.append("#ff9800")
        vline_widths.append(2.0)
        vline_alpha.append(1.0)

    vlines_kwargs = {}
    if vline_times:
        vlines_kwargs["vlines"] = dict(
            vlines=vline_times,
            colors=vline_colors,
            linewidths=vline_widths,
            alpha=vline_alpha,
            linestyle="--",
        )

    # ── Plot ────────────────────────────────────────────────────────────
    title = f"{ticker}   {date}   +{mom_pct:.2f}%     ({n_bars:,} bars · {n_days} days)"

    # Wider figure for multi-day span
    fig_w = max(20, min(32, n_bars // 30))

    fig, axes = mpf.plot(
        bars,
        type="candle",
        style=_STYLE,
        title=title,
        ylabel="Price ($)",
        volume=True,
        ylabel_lower="Volume",
        figsize=(fig_w, 9),
        tight_layout=True,
        returnfig=True,
        datetime_format="%m/%d %H:%M",
        xrotation=45,
        warn_too_much_data=10000,
        **vlines_kwargs,
    )

    for ax in fig.axes:
        ax.title.set_color("#eeeeee")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(
        out_path, dpi=110,
        bbox_inches="tight",
        facecolor=fig.get_facecolor(),
    )
    plt.close(fig)
    return "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(EVENT_FILE) as f:
        data = json.load(f)
    events = data["events"]
    total  = len(events)

    print(f"Generating {total} full-span charts -> {OUT_DIR}")
    print(f"  Gray dashed  = 4am day boundary")
    print(f"  Cyan dashed  = 09:30 RTH open on event date")
    print(f"  Orange dashed = scanner hit")
    print()

    ok = skipped = errors = 0
    for idx, ev in enumerate(events, 1):
        dir_name = ev["dir_name"]
        out_path = OUT_DIR / f"{dir_name}.png"

        if out_path.exists() and not args.overwrite:
            print(f"  [{idx:3d}/{total}] SKIP {dir_name} (exists)")
            skipped += 1
            continue

        print(f"  [{idx:3d}/{total}] {dir_name} ...", end=" ", flush=True)
        try:
            status = _make_chart(ev, out_path)
        except Exception as e:
            status = f"err:{e}"
        finally:
            plt.close("all")

        if status == "ok":
            print("OK")
            ok += 1
        elif status.startswith("skip:"):
            print(f"SKIP ({status[5:]})")
            skipped += 1
        else:
            print(f"ERROR — {status[4:]}")
            errors += 1

    print()
    print(f"Done: {ok} rendered, {skipped} skipped, {errors} errors")
    print(f"Output: {OUT_DIR}")


if __name__ == "__main__":
    main()
