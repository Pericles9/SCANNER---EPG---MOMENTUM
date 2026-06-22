#!/usr/bin/env python3
"""R1 T3 — Per-event Plotly charts for selected asymmetric gate configs.

Runs against the two Cooper-selected T2 configs:
  po70_pc65      p_open=0.70, p_close=0.65
  low_close_0.40 p_open=0.65, p_close=0.40

Reads per_trade.json from each config's T2 results directory.
Writes charts to results/phase_r1/t3_charts_{label}/.
"""
from __future__ import annotations

import json
import math
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKTEST))

from data.schemas.mom_db import CONFIG_DIR
from data.loaders.trades import list_events, _session_ns_bounds
from phase_r1_diag import (
    _collect_event_diag,
    generate_chart,
    generate_index,
    REPO,
    RESULTS_DIR,
)

CONFIGS = [
    (0.70, 0.65, "po70_pc65"),
    (0.65, 0.40, "low_close_0.40"),
]

# Set to a specific label to run only that config, or None to run all.
_ONLY_LABEL: str | None = None


def run_config(p_open: float, p_close: float, label: str) -> None:
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Load traded events from this config's T2 per_trade.json
    t2_path = RESULTS_DIR / f"asymmetric_{label}" / "per_trade.json"
    if not t2_path.exists():
        print(f"ERROR: {t2_path} not found", file=sys.stderr)
        return
    with open(t2_path) as f:
        t2_trades = json.load(f)
    traded_keys = {(tr["ticker"], tr["date"]) for tr in t2_trades}
    print(f"  {label}: {len(traded_keys)} traded events")

    # Val event lookup
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    all_events = list_events(min_mom=50.0, require_date=True)
    val_lookup = {
        (e["ticker"], e["date"]): e
        for e in all_events
        if val_start <= e["date"] < test_start
    }

    # Per-event Hawkes params
    phase_a_path = REPO / "results" / "phase_a" / "production_fit_results.json"
    per_event_params: dict = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    args_list = []
    for ticker, date in sorted(traded_keys):
        ev = val_lookup.get((ticker, date))
        if ev is None:
            print(f"  WARNING: {ticker} {date} not in val events — skipping")
            continue
        fp = per_event_params.get((ticker, date), hawkes_median)
        args_list.append({
            "ticker": ticker,
            "date": date,
            "mom_pct": ev["mom_pct"],
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
            "p_open": p_open,
            "p_close": p_close,
        })

    print(f"  Processing {len(args_list)} events (6 workers)...")
    raw_results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_collect_event_diag, a): a for a in args_list}
        done = 0
        for future in as_completed(futures):
            done += 1
            r = future.result()
            raw_results.append(r)
            if done % 10 == 0:
                print(f"    {done}/{len(args_list)} events processed...")

    event_results = [r for r in raw_results if r.get("status") == "event"]
    traded_results = [r for r in event_results if r.get("has_trade")]
    errors = [r for r in raw_results if r.get("status") == "error"]
    if errors:
        print(f"  {len(errors)} errors:")
        for e in errors[:5]:
            print(f"    {e['ticker']} {e.get('session_date', '?')}: {e.get('error', '')[:120]}")

    print(f"  {label}: {len(traded_results)} with trades, generating charts...")

    chart_dir = RESULTS_DIR / f"t3_charts_{label}"
    chart_dir.mkdir(parents=True, exist_ok=True)

    n_ok = 0
    for i, r in enumerate(traded_results):
        try:
            p = generate_chart(r, chart_dir)
            if p:
                n_ok += 1
            if (i + 1) % 10 == 0:
                print(f"    {i+1}/{len(traded_results)} charts generated")
        except Exception as e:
            print(f"  Chart failed {r['ticker']} {r['session_date']}: {e}")

    generate_index(event_results, chart_dir, p_open=p_open, p_close=p_close)
    print(f"  {label}: {n_ok}/{len(traded_results)} charts OK -> {chart_dir}")


def main() -> None:
    configs = CONFIGS if _ONLY_LABEL is None else [c for c in CONFIGS if c[2] == _ONLY_LABEL]
    for p_open, p_close, label in configs:
        print(f"\n=== T3: {label}  (p_open={p_open}, p_close={p_close}) ===")
        run_config(p_open, p_close, label)
    print("\nT3 COMPLETE")


if __name__ == "__main__":
    main()
