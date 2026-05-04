#!/usr/bin/env python3
"""Phase S — Batch runner for per-event signal charts.

For each of the 81 events that produced trades:
  1. Pull the event's trades from per_trade.parquet
  2. Replay the EPG state via `replay_epg_for_event`
  3. Resolve prev_close (cached on the trade record)
  4. Render the chart via `make_event_chart`

Failures are logged to chart_errors.json without stopping the batch.

Run serially -- 81 events.
"""
from __future__ import annotations

import json
import sys
import time
import traceback
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from backtest.charts import make_event_chart
from backtest.epg_replay import replay_epg_for_event


BASE = Path(__file__).resolve().parent.parent / "results" / "backtest"
CHARTS_DIR = BASE / "event_charts"


def main():
    CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    trades_df = pd.read_parquet(BASE / "per_trade.parquet")
    with open(BASE / "per_event_summary.json") as f:
        events = json.load(f)

    # Only events with at least one trade get a chart (per spec: 81 events).
    # `per_event_summary.json` includes events with status='event' that may
    # have zero trades (gap gate blocked everything). Use the trades index
    # to decide which events get a chart.
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
        out_path = CHARTS_DIR / f"{ticker}_{date}.html"
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
        err_path = CHARTS_DIR / "chart_errors.json"
        with open(err_path, "w") as f:
            json.dump(errors, f, indent=2)
        print(f"Errors written: {err_path}")


if __name__ == "__main__":
    main()
