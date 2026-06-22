#!/usr/bin/env python3
"""R1 T2 — Asymmetric EPG gate threshold sweep.

Original spec grid: p_open ∈ {0.60, 0.65, 0.70} × p_close ∈ {0.55, 0.60, 0.65, 0.70}
excluding symmetric arms already in T1 and invalid (p_close > p_open) arms.

Added by Cooper (chatter reduction): p_open=0.65 with p_close ∈ {0.40, 0.50}.

Valid asymmetric configs (p_close < p_open, non-symmetric from original grid):
  (0.60, 0.55)
  (0.65, 0.55), (0.65, 0.60)
  (0.70, 0.55), (0.70, 0.60), (0.70, 0.65)

Low-close added:
  (0.65, 0.40), (0.65, 0.50)

Total: 8 configs.
Reports same metrics as T1 plus mean_consecutive_pass_window_sec.
Writes results/phase_r1/asymmetric_sweep.json and asymmetric_chatter.json.
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

# (p_open, p_close, label)
CONFIGS = [
    # Original grid asymmetric arms (p_close < p_open)
    (0.60, 0.55, "po60_pc55"),
    (0.65, 0.55, "po65_pc55"),
    (0.65, 0.60, "po65_pc60"),
    (0.70, 0.55, "po70_pc55"),
    (0.70, 0.60, "po70_pc60"),
    (0.70, 0.65, "po70_pc65"),
    # Low-close arms added by Cooper
    (0.65, 0.40, "low_close_0.40"),
    (0.65, 0.50, "low_close_0.50"),
]


def run_config(p_open: float, p_close: float, label: str) -> dict:
    out_dir = RESULTS_DIR / f"asymmetric_{label}"
    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "cross_and_hold",
        "--n-hold", "3",
        "--p-open", str(p_open),
        "--p-close", str(p_close),
        "--no-gap-gate",
        "--split", "val",
        "--random-sample", "100",
        "--seed", "42",
        "--workers", "6",
        "--results-dir", str(out_dir),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(REPO))
    if result.returncode != 0:
        return {"p_open": p_open, "p_close": p_close, "label": label,
                "error": result.stderr[-2000:]}
    summary_path = out_dir / "run_summary.json"
    per_event_path = out_dir / "per_event_summary.json"
    if not summary_path.exists():
        return {"p_open": p_open, "p_close": p_close, "label": label,
                "error": "run_summary.json not found"}
    with open(summary_path) as f:
        summary = json.load(f)
    per_event = []
    if per_event_path.exists():
        with open(per_event_path) as f:
            per_event = json.load(f)
    return {"p_open": p_open, "p_close": p_close, "label": label,
            "summary": summary, "per_event": per_event}


def build_chatter_diagnostic(results: list[dict]) -> list[dict]:
    rows = []
    for r in results:
        if "error" in r:
            continue
        per_event = r.get("per_event", [])
        transitions = [ev.get("n_passtofail_transitions", 0) for ev in per_event]
        n_events = len(transitions)
        if n_events == 0:
            continue
        counts = Counter(transitions)
        n_high = sum(v for k, v in counts.items() if k >= 3)
        rows.append({
            "p_open": r["p_open"],
            "p_close": r["p_close"],
            "label": r["label"],
            "n_events": n_events,
            "mean_passtofail": round(sum(transitions) / n_events, 3),
            "pct_zero_transitions": round(100 * counts.get(0, 0) / n_events, 1),
            "pct_one_transition": round(100 * counts.get(1, 0) / n_events, 1),
            "pct_two_transitions": round(100 * counts.get(2, 0) / n_events, 1),
            "pct_ge3_transitions": round(100 * n_high / n_events, 1),
            "transition_distribution": dict(sorted(counts.items())),
        })
    return rows


def main():
    print(f"Running {len(CONFIGS)} asymmetric configs sequentially...")

    raw_results = []
    for p_open, p_close, label in CONFIGS:
        print(f"  Running p_open={p_open:.2f} p_close={p_close:.2f} ({label})...",
              end=" ", flush=True)
        try:
            r = run_config(p_open, p_close, label)
        except Exception as e:
            r = {"p_open": p_open, "p_close": p_close, "label": label, "error": str(e)}
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
                f"chatter={s.get('mean_passtofail_per_event')} "
                f"pass_win={s.get('mean_consecutive_pass_window_sec')}s"
            )

    # Build sweep table
    sweep_rows = []
    for r in raw_results:
        if "error" in r:
            sweep_rows.append({"p_open": r["p_open"], "p_close": r["p_close"],
                                "label": r["label"], "error": r["error"][:200]})
            continue
        s = r["summary"]
        sweep_rows.append({
            "p_open": r["p_open"],
            "p_close": r["p_close"],
            "label": r["label"],
            "n_trades": s.get("n_trades"),
            "profit_factor": s.get("profit_factor"),
            "win_rate": s.get("win_rate"),
            "mean_pnl_pct": s.get("mean_pnl_pct"),
            "cvar5_pct": s.get("cvar5_pct"),
            "mean_hold_sec": s.get("mean_hold_sec"),
            "mean_entry_lag_sec": s.get("mean_entry_lag_sec"),
            "mean_passtofail_per_event": s.get("mean_passtofail_per_event"),
            "mean_consecutive_pass_window_sec": s.get("mean_consecutive_pass_window_sec"),
            "exit_reason_breakdown": s.get("exit_reason_breakdown", {}),
        })

    sweep_path = RESULTS_DIR / "asymmetric_sweep.json"
    with open(sweep_path, "w") as f:
        json.dump(sweep_rows, f, indent=2)
    print(f"\nSweep written to {sweep_path}")

    chatter = build_chatter_diagnostic(raw_results)
    chatter_path = RESULTS_DIR / "asymmetric_chatter.json"
    with open(chatter_path, "w") as f:
        json.dump(chatter, f, indent=2)
    print(f"Chatter written to {chatter_path}")


if __name__ == "__main__":
    main()
