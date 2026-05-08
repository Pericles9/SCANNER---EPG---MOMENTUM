#!/usr/bin/env python3
"""Phase A — Batch runner for per-event signal charts.

For each event that produced trades:
  1. Pull the event's trades from per_trade.parquet
  2. Replay the EPG + LULD state via `replay_epg_for_event`
  3. Resolve prev_close (cached on the trade record)
  4. Render the chart via `make_event_chart`

Failures are logged to chart_errors.json without stopping the batch.

Usage:
    python -m backtest.run_charts \
        --results-dir results/phase_a \
        --output-dir results/phase_a/charts
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.charts import make_event_chart
from backtest.epg_replay import replay_epg_for_event


DEFAULT_BASE = Path(__file__).resolve().parent.parent / "results" / "backtest"


def parse_args():
    parser = argparse.ArgumentParser(description="Phase A — per-event chart batch runner")
    parser.add_argument("--results-dir", type=str, default=None,
                        help="Directory containing per_trade.parquet and per_event_summary.json")
    parser.add_argument("--output-dir", type=str, default=None,
                        help="Directory to write chart HTML files (default: <results-dir>/charts)")
    return parser.parse_args()


def main():
    args = parse_args()
    base = Path(args.results_dir) if args.results_dir else DEFAULT_BASE
    charts_dir = (Path(args.output_dir) if args.output_dir
                  else base / "event_charts")
    charts_dir.mkdir(parents=True, exist_ok=True)

    trades_df = pd.read_parquet(base / "per_trade.parquet")
    with open(base / "per_event_summary.json") as f:
        events = json.load(f)

    trade_event_keys = set(zip(trades_df["ticker"], trades_df["date"]))
    chartable = [e for e in events
                 if (e["ticker"], e["date"]) in trade_event_keys]

    print(f"Total events with trades: {len(chartable)}")

    errors = []
    t0 = time.time()
    for idx, ev in enumerate(chartable, 1):
        ticker = ev["ticker"]
        date = ev["date"]
        prev_close = float(ev["prev_close"])
        out_path = charts_dir / f"{ticker}_{date}.html"
        ev_trades = trades_df[
            (trades_df["ticker"] == ticker) & (trades_df["date"] == date)
        ].copy()

        print(f"[{idx}/{len(chartable)}] {ticker} {date} "
              f"({len(ev_trades)} trades) ...", end=" ", flush=True)

        try:
            t_ev0 = time.time()
            epg_data = replay_epg_for_event(ticker, date)
            t_ev1 = time.time()
            make_event_chart(
                ticker=ticker, date=date,
                trades=ev_trades, epg_data=epg_data,
                prev_close=prev_close,
                output_path=str(out_path),
                luld_data=epg_data.get("luld_timeline"),
            )
            t_ev2 = time.time()
            size_kb = out_path.stat().st_size / 1024.0
            print(f"OK ({size_kb:.0f} KB, replay {t_ev1-t_ev0:.1f}s, "
                  f"chart {t_ev2-t_ev1:.1f}s)")
        except Exception as e:
            tb = traceback.format_exc()
            print(f"FAIL: {e}")
            errors.append({
                "ticker": ticker, "date": date,
                "error": str(e), "traceback": tb,
            })

    elapsed = time.time() - t0
    print(f"\nBatch complete: {len(chartable) - len(errors)}/{len(chartable)} "
          f"charts written in {elapsed:.0f}s")

    if errors:
        err_path = charts_dir / "chart_errors.json"
        with open(err_path, "w") as f:
            json.dump(errors, f, indent=2)
        print(f"Errors written: {err_path}")

    return len(errors)


if __name__ == "__main__":
    sys.exit(main())
