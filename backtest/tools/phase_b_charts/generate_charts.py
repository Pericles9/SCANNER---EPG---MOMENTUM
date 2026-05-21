"""Phase B per-event chart runner (T3).

Iterates all 81 traded events, loads replay cache + trade slice, calls
build_chart(), writes standalone HTML to event_charts/.

Usage:
    python -m tools.phase_b_charts.generate_charts
    python -m tools.phase_b_charts.generate_charts --tickers KAVL SOUN --dry-run
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from tools.exit_d_tuning.replay import _load_cache
from tools.phase_b_charts.chart import build_chart

# ── Paths ─────────────────────────────────────────────────────────────────
_ROOT = Path(__file__).resolve().parents[2]
_RESULTS_DIR = _ROOT / "results" / "phase_b" / "100_val_seed42"
_PHASE_B_CACHE = _ROOT / "results" / "phase_b" / "replay_caches"
_PHASE_U_CACHE = Path(r"D:\Trading Research\hawkes-ofi-impact\results\phase_u\replay_caches")
_OUT_DIR = _RESULTS_DIR / "event_charts"
_AUDIT_PATH = _OUT_DIR / "cache_audit.json"
_ERRORS_PATH = _OUT_DIR / "chart_errors.json"


def _get_cache_source(ticker: str, date: str, audit_map: dict) -> str:
    return audit_map.get((ticker, date), "missing")


def _load_replay(ticker: str, date: str, cache_source: str):
    if cache_source == "phase_b":
        return _load_cache(_PHASE_B_CACHE, ticker, date)
    elif cache_source == "phase_u":
        return _load_cache(_PHASE_U_CACHE, ticker, date)
    return None


def main(tickers: list[str] | None = None, dry_run: bool = False) -> None:
    _OUT_DIR.mkdir(parents=True, exist_ok=True)

    with open(_RESULTS_DIR / "per_event_summary.json") as f:
        all_events = json.load(f)

    with open(_AUDIT_PATH) as f:
        audit = json.load(f)
    audit_map = {(a["ticker"], a["date"]): a["cache_source"] for a in audit}

    per_trade = pd.read_parquet(_RESULTS_DIR / "per_trade.parquet")

    # Filter to specific tickers if requested
    if tickers:
        all_events = [e for e in all_events if e["ticker"] in tickers]

    n_total = len(all_events)
    print(f"Phase B chart runner — {n_total} events -> {_OUT_DIR}/")
    print()

    errors = []
    n_written = 0
    total_start = time.time()

    for idx, ev in enumerate(all_events, 1):
        ticker = ev["ticker"]
        date = ev["date"]
        ev_start = time.time()
        cache_source = _get_cache_source(ticker, date, audit_map)

        print(f"[{idx}/{n_total}] {ticker} {date} ({cache_source})", end="", flush=True)

        if dry_run:
            print(" [DRY RUN]")
            continue

        out_path = _OUT_DIR / f"{ticker}_{date}.html"

        try:
            replay = _load_replay(ticker, date, cache_source)
            if replay is None:
                raise FileNotFoundError(
                    f"No valid replay cache for {ticker} {date} (source={cache_source})"
                )

            trades_slice = per_trade[
                (per_trade["ticker"] == ticker) & (per_trade["date"] == date)
            ].copy()

            fig = build_chart(ticker, date, replay, trades_slice, ev)
            fig.write_html(str(out_path), include_plotlyjs="cdn")

            elapsed = time.time() - ev_start
            n_written += 1
            print(f"  OK  {elapsed:.1f}s")

        except Exception as exc:
            elapsed = time.time() - ev_start
            print(f"  ERROR  {elapsed:.1f}s  {exc}")
            errors.append({
                "ticker": ticker,
                "date": date,
                "error": str(exc),
                "elapsed_sec": round(elapsed, 2),
            })

    total_elapsed = time.time() - total_start

    # Write error log
    with open(_ERRORS_PATH, "w") as f:
        json.dump(errors, f, indent=2)

    print()
    print(f"Done. {n_written}/{n_total} charts written | {len(errors)} errors | "
          f"total {total_elapsed:.1f}s")
    if errors:
        print("Errors:")
        for e in errors:
            print(f"  {e['ticker']} {e['date']}: {e['error']}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase B chart runner")
    parser.add_argument("--tickers", nargs="+", default=None,
                        help="Limit to specific tickers")
    parser.add_argument("--dry-run", action="store_true",
                        help="List events without generating charts")
    args = parser.parse_args()
    main(tickers=args.tickers, dry_run=args.dry_run)
