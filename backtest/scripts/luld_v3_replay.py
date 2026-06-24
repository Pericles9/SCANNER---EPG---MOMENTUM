"""Phase LULD-V3 T4/T5 — Full-session LULD replay with confusion-matrix scoring.

Replays LuldProximityExit through the complete trading session for each
val-sample event (no strategy entry/exit logic) and scores each fire event
against the halt labels produced by luld_v3_label.py.

Usage
-----
# T4 baseline (V2 config: proximity_threshold=0.010, duration=0)
python -m backtest.scripts.luld_v3_replay --proximity-threshold 0.010 --duration 0 --tag t4_baseline

# T5 sweep: duration in seconds
python -m backtest.scripts.luld_v3_replay --proximity-threshold 0.010 --duration 4 --tag t5_dur4
python -m backtest.scripts.luld_v3_replay --proximity-threshold 0.010 --duration 8 --tag t5_dur8

Reads:
  results/phase_luld_v3/halt_labels.json    (from luld_v3_label.py)
  data/filtered/{TICKER}_{DATE}_{MOM}/trades.parquet
  data/filtered/{TICKER}_{DATE}_{MOM}/quotes.parquet

Writes:
  results/phase_luld_v3/{tag}_score.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

_HERE = Path(__file__).resolve().parent.parent   # backtest/
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent))

import numpy as np

from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes
from core.exits.luld_proximity import LuldProximityExit, ProximityState
from core.exits.luld_scoring import FireEvent, HaltLabel, aggregate_scores, score_fires

RESULTS_DIR = _HERE / "results" / "phase_luld_v3"

# Default V3 LULD config — can be overridden via CLI
DEFAULT_PROXIMITY_THRESHOLD = 0.010
DEFAULT_REF_WINDOW_SEC = 300.0
DEFAULT_WARMUP_SEC = 60.0

# Scoring weights (V3 spec defaults)
W_RECALL = 3.0
W_FP = 1.0
W_LIQ = 1.0
POSITION_VALUE_USD = 1000.0
PRE_HALT_WINDOW_SEC = 15.0


def _replay_event(
    ticker: str,
    date: str,
    mom_pct: float,
    proximity_threshold: float,
    ref_window_sec: float,
    warmup_sec: float,
    luld_exit_duration_sec: float,
) -> Optional[List[FireEvent]]:
    """Replay LULD module through full session; return list of first-tick fires."""
    td = load_trades(ticker, date, mom_pct)
    if td is None or td.n_trades < 30:
        return None

    qd = load_quotes(ticker, date, mom_pct)
    if qd is None or qd.n_quotes < 10:
        return None

    luld = LuldProximityExit(
        ref_window_sec=ref_window_sec,
        proximity_threshold=proximity_threshold,
        warmup_sec=warmup_sec,
        luld_exit_duration_sec=luld_exit_duration_sec,
    )

    fires: List[FireEvent] = []
    nq = qd.n_quotes
    q_idx = 0
    prev_state = ProximityState.INACTIVE

    for i in range(td.n_trades):
        # Advance quote pointer to prevailing quote at this trade timestamp
        while q_idx < nq - 1 and qd.timestamps[q_idx + 1] <= td.timestamps[i]:
            q_idx += 1

        if q_idx < nq and qd.timestamps[q_idx] <= td.timestamps[i]:
            bid_q = float(qd.bid_prices[q_idx])
            ask_q = float(qd.ask_prices[q_idx])
            bid_sz = float(qd.bid_sizes[q_idx])
        else:
            bid_q = None
            ask_q = None
            bid_sz = 0.0

        result = luld.update(int(td.timestamps[i]), float(td.prices[i]), bid_q, ask_q)

        # Collect first-tick EXIT_HALT transitions (SAFE→EXIT_HALT or INACTIVE→EXIT_HALT)
        if result.state == ProximityState.EXIT_HALT and prev_state != ProximityState.EXIT_HALT:
            if bid_q is not None and ask_q is not None and bid_q > 0 and ask_q > bid_q:
                mid = (bid_q + ask_q) / 2.0
                spread_bps = (ask_q - bid_q) / mid * 10_000.0
                effective_bid_sz = bid_sz
            elif float(td.prices[i]) > 0:
                # Fallback: no valid quote, use trade price as mid, zero bid size
                mid = float(td.prices[i])
                spread_bps = 0.0
                effective_bid_sz = 0.0
            else:
                prev_state = result.state
                continue

            fires.append(FireEvent(
                timestamp_ns=int(td.timestamps[i]),
                spread_bps=spread_bps,
                bid_size_shares=effective_bid_sz,
                mid_price=mid,
            ))

        prev_state = result.state

    return fires


def main() -> None:
    parser = argparse.ArgumentParser(description="LULD V3 replay + scoring")
    parser.add_argument("--proximity-threshold", type=float,
                        default=DEFAULT_PROXIMITY_THRESHOLD)
    parser.add_argument("--duration", type=float, default=0.0,
                        help="luld_exit_duration_sec (0=immediate/V2 behaviour)")
    parser.add_argument("--ref-window-sec", type=float, default=DEFAULT_REF_WINDOW_SEC)
    parser.add_argument("--warmup-sec", type=float, default=DEFAULT_WARMUP_SEC)
    parser.add_argument("--tag", type=str, required=True,
                        help="Output file tag, e.g. t4_baseline or t5_dur6")
    parser.add_argument("--labels", type=str,
                        default=str(RESULTS_DIR / "halt_labels.json"),
                        help="Path to halt_labels.json from luld_v3_label.py")
    args = parser.parse_args()

    labels_path = Path(args.labels)
    if not labels_path.exists():
        print(f"ERROR: halt_labels.json not found at {labels_path}")
        print("Run scripts/luld_v3_label.py first (T2).")
        sys.exit(1)

    with open(labels_path) as f:
        label_data = json.load(f)

    events = label_data["events"]
    print(
        f"Config: proximity_threshold={args.proximity_threshold:.4f} "
        f"duration={args.duration:.1f}s "
        f"ref_window={args.ref_window_sec:.0f}s warmup={args.warmup_sec:.0f}s"
    )
    print(f"Events: {len(events)} | tag: {args.tag}")

    per_event_scores = []
    per_event_detail = []
    n_skipped = 0
    n_errors = 0

    for ev in events:
        ticker, date = ev["ticker"], ev["date"]
        if ev.get("skipped"):
            n_skipped += 1
            continue

        # Build halt labels for this event
        halt_labels = [
            HaltLabel(start_sec=h["start_sec"], end_sec=h["end_sec"], reason=h["reason"])
            for h in ev.get("halts", [])
        ]

        try:
            fires = _replay_event(
                ticker=ticker,
                date=date,
                mom_pct=ev.get("mom_pct", 0.0),
                proximity_threshold=args.proximity_threshold,
                ref_window_sec=args.ref_window_sec,
                warmup_sec=args.warmup_sec,
                luld_exit_duration_sec=args.duration,
            )
        except Exception as exc:
            print(f"  ERROR {ticker} {date}: {exc}")
            n_errors += 1
            continue

        if fires is None:
            n_skipped += 1
            continue

        es = score_fires(
            fires=fires,
            halts=halt_labels,
            pre_halt_window_sec=PRE_HALT_WINDOW_SEC,
            w_recall=W_RECALL,
            w_fp=W_FP,
            w_liq=W_LIQ,
            position_value_usd=POSITION_VALUE_USD,
        )
        per_event_scores.append(es)
        per_event_detail.append({
            "ticker": ticker,
            "date": date,
            "n_fires": es.n_fires,
            "n_halts": es.n_halts,
            "tp": es.tp,
            "fp": es.fp,
            "fn": es.fn,
            "recall": round(es.recall, 4),
            "precision": round(es.precision, 4),
            "fp_rate": round(es.fp_rate, 4),
            "mean_liq_penalty": round(es.mean_liq_penalty, 4),
            "composite": round(es.composite, 4),
        })

    agg = aggregate_scores(per_event_scores)

    print(f"\n{'='*60}")
    print(f"AGGREGATE  n_events={len(per_event_scores)} | skipped={n_skipped} | errors={n_errors}")
    print(f"  fires={agg.n_fires}  halts={agg.n_halts}")
    print(f"  TP={agg.tp}  FP={agg.fp}  FN={agg.fn}")
    print(f"  recall={agg.recall:.4f}  precision={agg.precision:.4f}  fp_rate={agg.fp_rate:.4f}")
    print(f"  mean_liq_penalty={agg.mean_liq_penalty:.2f}")
    print(f"  composite={agg.composite:.4f}")
    print(f"{'='*60}")

    output = {
        "config": {
            "proximity_threshold": args.proximity_threshold,
            "luld_exit_duration_sec": args.duration,
            "ref_window_sec": args.ref_window_sec,
            "warmup_sec": args.warmup_sec,
            "pre_halt_window_sec": PRE_HALT_WINDOW_SEC,
            "w_recall": W_RECALL,
            "w_fp": W_FP,
            "w_liq": W_LIQ,
            "position_value_usd": POSITION_VALUE_USD,
        },
        "aggregate": {
            "n_events_scored": len(per_event_scores),
            "n_events_skipped": n_skipped,
            "n_events_errored": n_errors,
            "n_fires": agg.n_fires,
            "n_halts": agg.n_halts,
            "tp": agg.tp,
            "fp": agg.fp,
            "fn": agg.fn,
            "recall": round(agg.recall, 4),
            "precision": round(agg.precision, 4),
            "fp_rate": round(agg.fp_rate, 4),
            "mean_liq_penalty": round(agg.mean_liq_penalty, 4),
            "composite": round(agg.composite, 4),
        },
        "per_event": per_event_detail,
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = RESULTS_DIR / f"{args.tag}_score.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    main()
