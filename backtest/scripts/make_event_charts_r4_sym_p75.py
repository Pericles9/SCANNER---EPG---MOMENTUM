"""
Generate 1-minute candlestick chart PNGs for all 100 val_r4_stratified events,
using the best-performing R1 config (sym_p75, p_open=p_close=0.75).

Reuses _load_all_bars, _STYLE, _MC from make_event_charts.py.
Adds entry (green) + exit (red) vlines from sym_p75 per_trade.json.
Stratum and outcome annotated in chart title.

Output: backtest/results/event_charts_r4_sym_p75/{dir_name}.png

Usage:
    python backtest/scripts/make_event_charts_r4_sym_p75.py
    python backtest/scripts/make_event_charts_r4_sym_p75.py --overwrite
"""
import argparse
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")

import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKTEST = PROJECT_ROOT / "backtest"
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from data.schemas.mom_db import FILTERED_DIR  # noqa: E402
from backtest.scripts.make_event_charts import _load_all_bars, _STYLE  # noqa: E402

SAMPLE     = BACKTEST / "data" / "val_r4_stratified.json"
TRADES_F   = BACKTEST / "results" / "phase_r1_final" / "sym_p75" / "per_trade.json"
OUT_DIR    = BACKTEST / "results" / "event_charts_r4_sym_p75"
ET         = "America/New_York"


def _find_dir_name(ticker: str, date: str) -> str | None:
    candidates = sorted(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    return candidates[-1].name if candidates else None


def _build_trade_index(trades_path: Path) -> dict:
    """Return {(ticker, date): [(entry_ts_ns, exit_ts_ns, pnl_pct), ...]}."""
    idx: dict = {}
    if not trades_path.exists():
        return idx
    for t in json.load(open(trades_path)):
        key = (t["ticker"], t["date"])
        idx.setdefault(key, []).append(
            (int(t["entry_ts"]), int(t["exit_ts"]), float(t["pnl_pct"]))
        )
    return idx


def _make_chart(ev: dict, trades: list, out_path: Path) -> str:
    ticker   = ev["ticker"]
    date     = ev["date"]
    mom_pct  = ev["mom_pct"]
    stratum  = ev.get("stratum", "?")
    dir_name = ev["dir_name"]

    bars = _load_all_bars(dir_name)
    if bars.empty or len(bars) < 5:
        return f"skip:only {len(bars)} bars"

    bars.index.name = "Date"
    n_days = bars.index.normalize().nunique()
    n_bars = len(bars)

    vline_times  = []
    vline_colors = []
    vline_widths = []
    vline_alpha  = []

    # Day boundaries (gray)
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

    # Scanner hit (orange)
    hit_ns = ev.get("scanner_hit_ts_ns")
    if hit_ns:
        hit_ts = pd.Timestamp(int(hit_ns), unit="ns", tz="UTC").tz_convert(ET)
        vline_times.append(hit_ts)
        vline_colors.append("#ff9800")
        vline_widths.append(2.0)
        vline_alpha.append(1.0)

    # Entry (green) + exit (red) per trade in this event
    for entry_ns, exit_ns, pnl in trades:
        entry_ts = pd.Timestamp(entry_ns, unit="ns", tz="UTC").tz_convert(ET)
        exit_ts  = pd.Timestamp(exit_ns,  unit="ns", tz="UTC").tz_convert(ET)
        vline_times.append(entry_ts)
        vline_colors.append("#00e676")
        vline_widths.append(1.8)
        vline_alpha.append(0.95)
        vline_times.append(exit_ts)
        vline_colors.append("#ff1744" if pnl < 0 else "#ff6d00")
        vline_widths.append(1.8)
        vline_alpha.append(0.95)

    vlines_kwargs = {}
    if vline_times:
        vlines_kwargs["vlines"] = dict(
            vlines=vline_times,
            colors=vline_colors,
            linewidths=vline_widths,
            alpha=vline_alpha,
            linestyle="--",
        )

    traded_str = ""
    if trades:
        pnls = [p for _, _, p in trades]
        traded_str = f"  TRADED {'+' if pnls[0] >= 0 else ''}{pnls[0]:.2f}%"

    title = (
        f"{ticker}   {date}   +{mom_pct:.2f}%   [{stratum}]"
        f"{traded_str}     ({n_bars:,} bars · {n_days} days)"
    )

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
    fig.savefig(out_path, dpi=110, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return "ok"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)

    events = json.load(open(SAMPLE))["events"]
    trade_idx = _build_trade_index(TRADES_F)
    total = len(events)

    print(f"Generating {total} charts -> {OUT_DIR}")
    print(f"  sym_p75 per_trade.json: {sum(len(v) for v in trade_idx.values())} trades across {len(trade_idx)} events")
    print(f"  Gray dashed   = 4am day boundary")
    print(f"  Cyan dashed   = 09:30 RTH open")
    print(f"  Orange dashed = scanner hit")
    print(f"  Green dashed  = entry")
    print(f"  Red/orange dashed = exit (red=loss, orange=win)")
    print()

    ok = skipped = errors = 0
    for idx, ev in enumerate(events, 1):
        dir_name = _find_dir_name(ev["ticker"], ev["date"])
        if dir_name is None:
            print(f"  [{idx:3d}/{total}] SKIP {ev['ticker']} {ev['date']} (no event dir)")
            skipped += 1
            continue

        ev["dir_name"] = dir_name
        out_path = OUT_DIR / f"{dir_name}.png"

        if out_path.exists() and not args.overwrite:
            print(f"  [{idx:3d}/{total}] SKIP {dir_name} (exists)")
            skipped += 1
            continue

        trades = trade_idx.get((ev["ticker"], ev["date"]), [])
        traded_tag = f" +trade" if trades else ""
        print(f"  [{idx:3d}/{total}] {dir_name}{traded_tag} ...", end=" ", flush=True)

        try:
            status = _make_chart(ev, trades, out_path)
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
