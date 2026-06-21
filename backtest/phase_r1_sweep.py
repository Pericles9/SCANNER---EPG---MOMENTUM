#!/usr/bin/env python3
"""R1 T1 — Symmetric EPG gate threshold sweep.

Runs 6 configs: p_open = p_close in {0.50, 0.55, 0.60, 0.65, 0.70, 0.75}.
Entry: cross_and_hold, n_hold=3, rho_fast=0.90 (default).
Writes results/phase_r1/symmetric_sweep.json and chatter_diagnostic.json.
"""
from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
REPO = Path(__file__).resolve().parent.parent
BACKTEST = Path(__file__).resolve().parent
RESULTS_DIR = BACKTEST / "results" / "phase_r1"
RESULTS_DIR.mkdir(parents=True, exist_ok=True)

P_VALUES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]


def run_config(p: float) -> dict:
    label = f"p{int(p * 100):02d}"
    out_dir = RESULTS_DIR / f"symmetric_{label}"
    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "cross_and_hold",
        "--n-hold", "3",
        "--p-open", str(p),
        "--p-close", str(p),
        "--no-gap-gate",
        "--split", "val",
        "--random-sample", "100",
        "--seed", "42",
        "--workers", "6",  # configs run sequentially; 6 inner workers each
        "--results-dir", str(out_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if result.returncode != 0:
        return {"p": p, "error": result.stderr[-2000:]}
    summary_path = out_dir / "run_summary.json"
    per_event_path = out_dir / "per_event_summary.json"
    if not summary_path.exists():
        return {"p": p, "error": "run_summary.json not found"}
    with open(summary_path) as f:
        summary = json.load(f)
    per_event = []
    if per_event_path.exists():
        with open(per_event_path) as f:
            per_event = json.load(f)
    return {"p": p, "summary": summary, "per_event": per_event}


def build_chatter_diagnostic(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        if "error" in r:
            continue
        p = r["p"]
        per_event = r.get("per_event", [])
        transitions = [ev.get("n_passtofail_transitions", 0) for ev in per_event]
        n_events = len(transitions)
        if n_events == 0:
            continue
        counts = Counter(transitions)
        n_high_chatter = sum(v for k, v in counts.items() if k >= 3)
        rows.append({
            "p": p,
            "n_events": n_events,
            "mean_passtofail": round(sum(transitions) / n_events, 3),
            "pct_zero_transitions": round(100 * counts.get(0, 0) / n_events, 1),
            "pct_one_transition": round(100 * counts.get(1, 0) / n_events, 1),
            "pct_two_transitions": round(100 * counts.get(2, 0) / n_events, 1),
            "pct_ge3_transitions": round(100 * n_high_chatter / n_events, 1),
            "transition_distribution": dict(sorted(counts.items())),
        })
    return rows


def main():
    print(f"Running {len(P_VALUES)} symmetric configs sequentially...")

    raw_results = []
    for p in P_VALUES:
        print(f"  Running p={p:.2f}...", end=" ", flush=True)
        try:
            r = run_config(p)
        except Exception as e:
            r = {"p": p, "error": str(e)}
        raw_results.append(r)
        if "error" in r:
            print(f"ERROR — {r['error'][:200]}")
        else:
            s = r["summary"]
            print(
                f"PF={s.get('profit_factor')} "
                f"n={s.get('n_trades')} win%={s.get('win_rate')} "
                f"CVaR5={s.get('cvar5_pct')} "
                f"lag={s.get('mean_entry_lag_sec')}s "
                f"chatter={s.get('mean_passtofail_per_event')}"
            )

    raw_results.sort(key=lambda r: r["p"])

    # Build sweep table (strip per_event to keep file small)
    sweep_rows = []
    for r in raw_results:
        if "error" in r:
            sweep_rows.append({"p": r["p"], "error": r["error"][:200]})
            continue
        s = r["summary"]
        sweep_rows.append({
            "p_open": r["p"],
            "p_close": r["p"],
            "n_trades": s.get("n_trades"),
            "profit_factor": s.get("profit_factor"),
            "win_rate": s.get("win_rate"),
            "mean_pnl_pct": s.get("mean_pnl_pct"),
            "cvar5_pct": s.get("cvar5_pct"),
            "mean_hold_sec": s.get("mean_hold_sec"),
            "mean_entry_lag_sec": s.get("mean_entry_lag_sec"),
            "mean_passtofail_per_event": s.get("mean_passtofail_per_event"),
            "exit_reason_breakdown": s.get("exit_reason_breakdown", {}),
        })

    sweep_path = RESULTS_DIR / "symmetric_sweep.json"
    with open(sweep_path, "w") as f:
        json.dump(sweep_rows, f, indent=2)
    print(f"\nSweep written to {sweep_path}")

    chatter = build_chatter_diagnostic(raw_results)
    chatter_path = RESULTS_DIR / "chatter_diagnostic.json"
    with open(chatter_path, "w") as f:
        json.dump(chatter, f, indent=2)
    print(f"Chatter diagnostic written to {chatter_path}")


if __name__ == "__main__":
    main()
