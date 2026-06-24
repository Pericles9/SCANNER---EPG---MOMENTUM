"""Phase LULD-V3 T2 — Ground-truth halt label generator.

Runs detect_luld_halts() with band_tier="tier2" and limit_state_seconds=15
against trades.parquet for the 100-event val sample (seed=42, split=val).

Writes: results/phase_luld_v3/halt_labels.json

Schema of halt_labels.json:
{
  "meta": {
    "band_tier": "tier2",
    "limit_state_seconds": 15,
    "halt_gap_seconds": 300,
    "sample_events": 100,
    "seed": 42,
    "split": "val",
    "n_events_with_halts": <int>,
    "total_halts": <int>
  },
  "events": [
    {
      "ticker": "AAPL",
      "date": "2024-01-15",
      "n_halts": 1,
      "halts": [
        {"start_sec": 1705330800.0, "end_sec": 1705331100.0, "reason": "luld"}
      ]
    },
    ...
  ]
}
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent.parent   # backtest/
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))            # scanner-epg-momentum/

import pyarrow.parquet as pq
import pandas as pd

from data.loaders.trades import list_events
from data.schemas.mom_db import CONFIG_DIR, FILTERED_DIR
from core.features.luld_halt_detection import detect_luld_halts

RESULTS_DIR = _HERE / "results" / "phase_luld_v3"

BAND_TIER = "tier2"
LIMIT_STATE_SECONDS = 15
HALT_GAP_SECONDS = 300
SAMPLE_N = 100
SEED = 42
SPLIT = "val"


def _load_val_sample() -> list[dict]:
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    all_events = list_events(min_mom=50.0, require_date=True)
    val_events = [
        ev for ev in all_events
        if val_start <= ev["date"] < test_start
    ]
    val_events.sort(key=lambda e: (e["date"], e["ticker"]))

    rng = random.Random(SEED)
    if len(val_events) >= SAMPLE_N:
        return rng.sample(val_events, SAMPLE_N)
    return val_events


def _load_trades_df(ticker: str, date: str, mom_pct: float) -> pd.DataFrame:
    dir_name = f"{ticker}_{date}_{mom_pct}"
    path = FILTERED_DIR / dir_name / "trades.parquet"
    if not path.exists():
        candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
        if not candidates:
            return pd.DataFrame()
        path = candidates[0] / "trades.parquet"
    table = pq.read_table(str(path), columns=["sip_timestamp", "price", "size"])
    df = table.to_pandas()
    df["sip_timestamp"] = pd.to_datetime(df["sip_timestamp"], unit="ns")
    df = df.sort_values("sip_timestamp").set_index("sip_timestamp")
    return df


def main() -> None:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    events = _load_val_sample()
    print(f"Loaded {len(events)} val-sample events (seed={SEED})")

    event_rows = []
    total_halts = 0
    n_with_halts = 0
    n_skipped = 0

    for ev in events:
        ticker, date, mom_pct = ev["ticker"], ev["date"], ev["mom_pct"]
        df = _load_trades_df(ticker, date, mom_pct)
        if df.empty:
            print(f"  SKIP {ticker} {date}: no trades.parquet")
            n_skipped += 1
            event_rows.append({
                "ticker": ticker, "date": date, "n_halts": 0,
                "halts": [], "skipped": True,
            })
            continue

        halts = detect_luld_halts(
            df,
            price_col="price",
            size_col="size",
            band_tier=BAND_TIER,
            limit_state_seconds=LIMIT_STATE_SECONDS,
            halt_gap_seconds=HALT_GAP_SECONDS,
        )

        halt_list = [
            {
                "start_sec": float(h.start.timestamp()),
                "end_sec": float(h.end.timestamp()),
                "reason": h.reason,
                "start_str": str(h.start),
                "end_str": str(h.end),
            }
            for h in halts
        ]
        if halt_list:
            n_with_halts += 1
            total_halts += len(halt_list)
            print(f"  {ticker} {date}: {len(halt_list)} halt(s) detected")

        event_rows.append({
            "ticker": ticker, "date": date,
            "n_halts": len(halt_list),
            "halts": halt_list,
            "skipped": False,
        })

    output = {
        "meta": {
            "band_tier": BAND_TIER,
            "limit_state_seconds": LIMIT_STATE_SECONDS,
            "halt_gap_seconds": HALT_GAP_SECONDS,
            "sample_events": len(events),
            "seed": SEED,
            "split": SPLIT,
            "n_events_with_halts": n_with_halts,
            "n_events_skipped": n_skipped,
            "total_halts": total_halts,
        },
        "events": event_rows,
    }

    out_path = RESULTS_DIR / "halt_labels.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")
    print(f"Events: {len(events)} total | {n_with_halts} with halts | {n_skipped} skipped")
    print(f"Total halt windows detected: {total_halts}")

    if total_halts == 0:
        print("\n*** HARD STOP: zero halts detected across all events. ***")
        print("Check band_tier, limit_state_seconds, and trades.parquet column names.")
        sys.exit(1)


if __name__ == "__main__":
    main()
