"""
Build scanner hit catalog for val-split events.

For every event in the val split (2023-11-17 to 2024-07-22), finds the first
trade where price >= prev_close * SCANNER_THRESHOLD (30%).

Uses data.loaders.prev_close.get_prev_close (same 3-source chain as runner_rapid):
  1. DuckDB daily_bars
  2. data/daily/{TICKER}_daily.parquet
  3. Last trade from prior event-day directory

Writes: data/filtered/scanner_hit_catalog.json
  {
    "TICKER:DATE": {
      "ticker": str,
      "date": str,
      "prev_close": float | null,
      "scanner_threshold": float | null,
      "scanner_hit_ts_ns": int | null,   # Unix nanoseconds at scanner hit tick
      "scanner_hit_tod_sec": float | null, # seconds from 4am ET
      "scanner_hit_price": float | null,
      "scanner_hit_idx": int | null,
      "notes": str
    }
  }
"""
from __future__ import annotations
import json
import sys
from pathlib import Path

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

import numpy as np

from data.loaders.trades import list_events, load_trades, _session_ns_bounds
from data.loaders.prev_close import get_prev_close
from data.schemas.mom_db import DATA_ROOT

SCANNER_THRESHOLD = 0.30   # 30% gain triggers scanner
NS_PER_SEC = 1_000_000_000

VAL_START  = "2023-11-17"
VAL_END    = "2024-07-23"   # exclusive

OUT_PATH = DATA_ROOT / "filtered" / "scanner_hit_catalog.json"


def main():
    all_events = list_events(min_mom=50.0, require_date=True)
    val_events = [
        e for e in all_events
        if VAL_START <= e["date"] < VAL_END
    ]
    print(f"Val split: {len(val_events)} events ({VAL_START} to {VAL_END})")

    # Load existing catalog to allow incremental updates
    if OUT_PATH.exists():
        with open(OUT_PATH) as f:
            catalog: dict[str, dict] = json.load(f)
        print(f"Loaded existing catalog with {len(catalog)} entries")
    else:
        catalog = {}

    n_found   = 0
    n_first   = 0
    n_missing = 0
    n_error   = 0

    for idx, ev in enumerate(val_events):
        ticker = ev["ticker"]
        date   = ev["date"]
        key    = f"{ticker}:{date}"

        if idx % 100 == 0:
            print(f"  {idx}/{len(val_events)}  "
                  f"(found={n_found} first={n_first} missing={n_missing} err={n_error})")

        try:
            prev_close = get_prev_close(ticker, date)
            if prev_close is None or prev_close <= 0:
                catalog[key] = {
                    "ticker": ticker, "date": date, "prev_close": None,
                    "scanner_threshold": None,
                    "scanner_hit_ts_ns": None, "scanner_hit_tod_sec": None,
                    "scanner_hit_price": None, "scanner_hit_idx": None,
                    "notes": "prev_close unavailable",
                }
                n_error += 1
                continue

            threshold = prev_close * (1.0 + SCANNER_THRESHOLD)
            td = load_trades(ticker, date, ev["mom_pct"])
            start_ns, _ = _session_ns_bounds(date)

            hit_ts_ns  = None
            hit_tod    = None
            hit_price  = None
            hit_idx    = None
            notes      = ""

            for i in range(td.n_trades):
                if td.prices[i] >= threshold:
                    hit_ts_ns = int(td.timestamps[i])
                    hit_tod   = float(td.timestamps[i] - start_ns) / NS_PER_SEC
                    hit_price = float(td.prices[i])
                    hit_idx   = i
                    if i == 0:
                        n_first += 1
                        notes = "hit at first trade (pre-market gap-up)"
                    else:
                        n_found += 1
                    break

            if hit_ts_ns is None:
                n_missing += 1
                max_p = float(np.max(td.prices))
                notes = f"price never reached {threshold:.4f}; max={max_p:.4f}"

            catalog[key] = {
                "ticker": ticker,
                "date": date,
                "prev_close": float(prev_close),
                "scanner_threshold": float(threshold),
                "scanner_hit_ts_ns": hit_ts_ns,
                "scanner_hit_tod_sec": round(hit_tod, 3) if hit_tod is not None else None,
                "scanner_hit_price": round(hit_price, 4) if hit_price is not None else None,
                "scanner_hit_idx": hit_idx,
                "notes": notes,
            }

        except Exception as exc:
            n_error += 1
            catalog[key] = {
                "ticker": ticker, "date": date, "prev_close": None,
                "scanner_threshold": None,
                "scanner_hit_ts_ns": None, "scanner_hit_tod_sec": None,
                "scanner_hit_price": None, "scanner_hit_idx": None,
                "notes": f"error: {exc}",
            }

    print(f"\nDone: {len(val_events)} val events processed")
    print(f"  hit found (intraday): {n_found}")
    print(f"  hit at first trade:   {n_first}")
    print(f"  price never reached:  {n_missing}")
    print(f"  errors/no prev_close: {n_error}")
    print(f"  total with hit:       {n_found + n_first} "
          f"({(n_found + n_first) / len(val_events) * 100:.1f}%)")

    with open(OUT_PATH, "w") as f:
        json.dump(catalog, f, indent=2)
    print(f"\nWritten to {OUT_PATH}")

    # XBP 2023-12-04 spot check
    for key in ("XBP:2023-12-04", "XBP:2023-11-27"):
        if key in catalog:
            rec = catalog[key]
            print(f"\n{key}:")
            for k, v in rec.items():
                print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
