"""Phase D threshold sweep runner.

Runs the backtest with --intra-window-watermark-threshold for each value in
[0.02, 0.03, 0.05, 0.07] on the 100-event val seed=42 sample.

Writes per-threshold results to results/phase_d/sweep_<threshold>/
Aggregates all thresholds to results/phase_d/threshold_sweep.json

Usage:
    python -m tools.phase_d.run_sweep
    python -m tools.phase_d.run_sweep --thresholds 0.05 0.07
    python -m tools.phase_d.run_sweep --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = Path(r"D:\Trading Research\.venv\Scripts\python.exe")
_CONFIG = _ROOT / "config" / "phase_d.json"
_OUT_ROOT = _ROOT / "results" / "phase_d"
_SWEEP_OUT = _OUT_ROOT / "threshold_sweep.json"

_THRESHOLDS = [0.02, 0.03, 0.05, 0.07]


def _run_threshold(threshold: float, dry_run: bool) -> dict:
    tag = f"{threshold:.2f}".replace(".", "_")
    results_dir = _OUT_ROOT / f"sweep_{tag}"

    cmd = [
        str(_PYTHON), "-m", "backtest.runner",
        "--split", "val",
        "--random-sample", "100",
        "--seed", "42",
        "--workers", "8",
        "--config", str(_CONFIG),
        "--no-gap-gate",
        "--intra-window-watermark-threshold", str(threshold),
        "--results-dir", str(results_dir),
    ]

    print(f"\n{'='*60}")
    print(f"Threshold {threshold:.0%} -> {results_dir.name}/")
    if dry_run:
        print("  [DRY RUN — skipping]")
        return {"threshold": threshold, "status": "dry_run"}
    print(f"  cmd: {' '.join(cmd[-6:])}")
    t0 = time.time()
    result = subprocess.run(cmd, cwd=str(_ROOT), capture_output=False)
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  ERROR: exit code {result.returncode} after {elapsed:.0f}s")
        return {"threshold": threshold, "status": "error",
                "returncode": result.returncode, "elapsed_sec": round(elapsed, 1)}
    print(f"  Done in {elapsed:.0f}s")
    return {"threshold": threshold, "status": "ok", "elapsed_sec": round(elapsed, 1)}


def _read_sweep_results(thresholds: list[float]) -> list[dict]:
    rows = []
    for threshold in thresholds:
        tag = f"{threshold:.2f}".replace(".", "_")
        results_dir = _OUT_ROOT / f"sweep_{tag}"
        summary_path = results_dir / "run_summary.json"
        if not summary_path.exists():
            rows.append({"threshold": threshold, "status": "missing"})
            continue
        with open(summary_path) as f:
            s = json.load(f)
        bf = s.get("backside_filter", {})
        n_trades = s.get("n_trades", 0)
        n_pass_edges = s.get("gap_gate", {}).get("pass_edges_total", 0)
        n_intra = bf.get("n_intra_window_blocks", 0)
        n_re_blocked = bf.get("n_re_entries_intra_blocked", 0)
        n_first_blocked = n_intra - n_re_blocked
        rows.append({
            "threshold": threshold,
            "status": "ok",
            "profit_factor": s.get("profit_factor"),
            "n_trades": n_trades,
            "win_rate": s.get("win_rate"),
            "mean_pnl_pct": s.get("mean_pnl_pct"),
            "n_intra_window_blocks": n_intra,
            "n_first_entries_blocked": n_first_blocked,
            "n_re_entries_blocked": n_re_blocked,
            "entries_blocked_pct": round(100 * n_intra / max(n_pass_edges, 1), 2),
            "re_entries_blocked_pct": round(
                100 * n_re_blocked / max(s.get("entry_type_breakdown", {})
                                         .get("reentry", {}).get("count", 1), 1), 2
            ),
            "n_events_with_trades": s.get("meta", {}).get("n_events_with_trades"),
            "elapsed_sec": s.get("meta", {}).get("elapsed_sec"),
        })
    return rows


def main(thresholds: list[float] | None = None, dry_run: bool = False) -> None:
    if thresholds is None:
        thresholds = _THRESHOLDS
    _OUT_ROOT.mkdir(parents=True, exist_ok=True)

    print(f"Phase D threshold sweep: {[f'{t:.0%}' for t in thresholds]}")
    print(f"Config: {_CONFIG}")
    print(f"Output root: {_OUT_ROOT}/")

    run_results = []
    for t in thresholds:
        r = _run_threshold(t, dry_run)
        run_results.append(r)

    if dry_run:
        print("\n[DRY RUN] No runs executed.")
        return

    print("\n\nAggregating results...")
    rows = _read_sweep_results(thresholds)

    with open(_SWEEP_OUT, "w") as f:
        json.dump(rows, f, indent=2)
    print(f"Written: {_SWEEP_OUT}")

    # Print summary table
    print("\nThreshold sweep results:")
    print(f"{'Threshold':>10} {'PF':>8} {'n_trades':>10} {'win%':>7} "
          f"{'n_blocks':>10} {'n_re_blocked':>14} {'entries_blocked%':>17}")
    print("-" * 80)
    for row in rows:
        if row.get("status") != "ok":
            print(f"{row['threshold']:>10.0%}  {row.get('status', '?')}")
            continue
        print(
            f"{row['threshold']:>10.0%} "
            f"{row['profit_factor']:>8.4f} "
            f"{row['n_trades']:>10} "
            f"{row['win_rate']:>7.2f} "
            f"{row['n_intra_window_blocks']:>10} "
            f"{row['n_re_entries_blocked']:>14} "
            f"{row['entries_blocked_pct']:>17.2f}%"
        )

    # Best threshold selection (by PF; prefer looser if within 0.05)
    valid = [r for r in rows if r.get("status") == "ok" and r.get("profit_factor")]
    if valid:
        best = max(valid, key=lambda r: r["profit_factor"])
        # Check if a looser threshold is within 0.05 PF
        sorted_by_t = sorted(valid, key=lambda r: r["threshold"])
        best_pf = best["profit_factor"]
        for row in sorted_by_t:
            if row["threshold"] > best["threshold"] and (best_pf - row["profit_factor"]) <= 0.05:
                best = row
                break
        print(f"\nBest threshold: {best['threshold']:.0%} "
              f"(PF={best['profit_factor']:.4f}, n_trades={best['n_trades']})")

        # Escalation: n_trades < 50
        if best["n_trades"] < 50:
            print(f"\nESCALATION: best threshold n_trades={best['n_trades']} < 50 — "
                  "filter too aggressive. Await instruction.")
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase D threshold sweep")
    parser.add_argument("--thresholds", nargs="+", type=float, default=None)
    parser.add_argument("--dry-run", action="store_true", default=False)
    args = parser.parse_args()
    main(thresholds=args.thresholds, dry_run=args.dry_run)
