"""Phase E sweep runner -- symmetric spread-multiple LULD proximity exit.

Runs backtest for N={1, 2, 3} spread multiples on 100-event val seed=42.
All other parameters held at Phase D best config (2% intra-window watermark,
gap gate disabled, EXIT_D theta=0.65 tau=4s, re-entry enabled).

Usage:
    python -m tools.phase_e.run_sweep
    python -m tools.phase_e.run_sweep --dry-run
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_PYTHON = sys.executable
_RUNNER = str(_ROOT / "backtest" / "runner.py")
_CONFIG = str(_ROOT / "config" / "phase_e.json")
_RESULTS_BASE = _ROOT / "results" / "phase_e"
_SWEEP_MULTIPLES = [1, 2, 3]
_INTRA_WINDOW_THRESHOLD = 0.02   # Phase D best
_ESCALATION_PF_MAX = 3.0
_ESCALATION_N_TRADES_MIN = 50


def _results_dir(n: int) -> Path:
    return _RESULTS_BASE / f"sweep_N{n}"


def _run_variant(n: int, dry_run: bool) -> dict | None:
    results_dir = _results_dir(n)
    results_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        _PYTHON, _RUNNER,
        "--split", "val",
        "--random-sample", "100",
        "--seed", "42",
        "--config", _CONFIG,
        "--no-gap-gate",
        "--intra-window-watermark-threshold", str(_INTRA_WINDOW_THRESHOLD),
        "--luld-n-spread-multiple", str(n),
        "--results-dir", str(results_dir),
    ]
    print(f"\n[N={n}] -> {results_dir.name}/")
    print("  " + " ".join(cmd))
    if dry_run:
        print("  [DRY RUN]")
        return None

    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=False)
    elapsed = time.time() - t0
    if proc.returncode != 0:
        print(f"  ERROR: runner returned exit code {proc.returncode} in {elapsed:.1f}s")
        sys.exit(1)
    print(f"  Done in {elapsed:.1f}s")

    summary_path = results_dir / "run_summary.json"
    if not summary_path.exists():
        print(f"  ERROR: {summary_path} not found after run")
        sys.exit(1)
    with open(summary_path) as f:
        return json.load(f)


def _extract_exit_counts(summary: dict, reasons: list[str]) -> dict[str, int]:
    breakdown = summary.get("exit_reason_breakdown", {})
    return {r: breakdown.get(r, {}).get("count", 0) for r in reasons}


def _exit_pf(summary: dict, reason: str) -> float | None:
    breakdown = summary.get("exit_reason_breakdown", {})
    if reason not in breakdown:
        return None
    return breakdown[reason].get("profit_factor")


def _exit_mean_pnl(summary: dict, reason: str) -> float | None:
    breakdown = summary.get("exit_reason_breakdown", {})
    if reason not in breakdown:
        return None
    return breakdown[reason].get("mean_pnl_pct")


def main(dry_run: bool = False) -> None:
    _RESULTS_BASE.mkdir(parents=True, exist_ok=True)

    summaries: dict[int, dict] = {}
    for n in _SWEEP_MULTIPLES:
        summary = _run_variant(n, dry_run)
        if dry_run:
            continue
        summaries[n] = summary

        pf = summary.get("profit_factor")
        n_trades = summary.get("n_trades", 0)

        print(f"\n  PF={pf}  n_trades={n_trades}")

        # Escalation checks after each run
        if pf is not None and pf > _ESCALATION_PF_MAX:
            print(f"\nESCALATION: N={n} PF={pf:.4f} > {_ESCALATION_PF_MAX} -- hard stop")
            print("Post results so far and await instruction.")
            _write_partial_sweep(summaries)
            sys.exit(2)

        if n_trades < _ESCALATION_N_TRADES_MIN:
            print(f"\nESCALATION: N={n} n_trades={n_trades} < {_ESCALATION_N_TRADES_MIN} -- hard stop")
            _write_partial_sweep(summaries)
            sys.exit(2)

    if dry_run:
        print("\n[DRY RUN complete]")
        return

    _write_sweep_table(summaries)


def _row(n: int, summary: dict) -> dict:
    luld_lower = _extract_exit_counts(summary, ["luld_lower"])["luld_lower"]
    luld_upper = _extract_exit_counts(summary, ["luld_upper"])["luld_upper"]
    luld_total = luld_lower + luld_upper

    # Combined luld PF and mean pnl across both sides
    bd = summary.get("exit_reason_breakdown", {})
    luld_wins = sum(
        bd.get(r, {}).get("profit_factor", 0) * bd.get(r, {}).get("count", 0)
        for r in ("luld_lower", "luld_upper")
    )

    # Compute combined LULD PF from raw trade parquet would be ideal, but
    # we can approximate from exit breakdown counts and PFs:
    # Use sum of PF*count weighted approach -- but since PF is a ratio of sums
    # this is only an approximation. The sweep tool will recompute exact from parquet.
    luld_pf = None
    luld_mean = None
    if "luld_lower" in bd or "luld_upper" in bd:
        # For now record individual sides; sweep_summary.py recomputes combined
        luld_lower_pf = _exit_pf(summary, "luld_lower")
        luld_upper_pf = _exit_pf(summary, "luld_upper")
        luld_lower_mean = _exit_mean_pnl(summary, "luld_lower")
        luld_upper_mean = _exit_mean_pnl(summary, "luld_upper")
    else:
        luld_lower_pf = luld_upper_pf = None
        luld_lower_mean = luld_upper_mean = None

    n_exit_d = _extract_exit_counts(summary, ["exit_d"])["exit_d"]
    exit_d_pf = _exit_pf(summary, "exit_d")
    n_epg = _extract_exit_counts(summary, ["epg_window_close"])["epg_window_close"]
    epg_pf = _exit_pf(summary, "epg_window_close")

    return {
        "n_spread_multiple": n,
        "PF": summary.get("profit_factor"),
        "n_trades": summary.get("n_trades"),
        "win_pct": summary.get("win_rate"),
        "mean_pnl_pct": summary.get("mean_pnl_pct"),
        "n_luld_lower": luld_lower,
        "n_luld_upper": luld_upper,
        "n_luld_total": luld_total,
        "luld_lower_pf": luld_lower_pf,
        "luld_upper_pf": luld_upper_pf,
        "luld_lower_mean_pnl_pct": luld_lower_mean,
        "luld_upper_mean_pnl_pct": luld_upper_mean,
        "n_exit_d": n_exit_d,
        "exit_d_pf": exit_d_pf,
        "n_epg_window_close": n_epg,
        "epg_close_pf": epg_pf,
        "luld_fallback_ticks": summary.get("luld_fallback_ticks"),
    }


def _write_partial_sweep(summaries: dict[int, dict]) -> None:
    rows = [_row(n, s) for n, s in sorted(summaries.items())]
    out = {"status": "partial", "rows": rows}
    out_path = _RESULTS_BASE / "sweep_summary_partial.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"Partial sweep written -> {out_path}")


def _write_sweep_table(summaries: dict[int, dict]) -> None:
    rows = [_row(n, s) for n, s in sorted(summaries.items())]
    out = {"sweep_multiples": _SWEEP_MULTIPLES, "rows": rows}
    out_path = _RESULTS_BASE / "sweep_summary.json"
    with open(out_path, "w") as f:
        json.dump(out, f, indent=2)
    print(f"\nSweep summary -> {out_path}")

    print("\nSweep results:")
    print(f"{'N':>3}  {'PF':>8}  {'n':>6}  {'win%':>6}  {'mean%':>7}  "
          f"{'luld_lo':>7}  {'luld_hi':>7}  {'exit_d':>7}  {'epg_cl':>7}")
    for row in rows:
        print(
            f"{row['n_spread_multiple']:>3}  "
            f"{row['PF']:>8.4f}  "
            f"{row['n_trades']:>6}  "
            f"{row['win_pct']:>6.2f}  "
            f"{row['mean_pnl_pct']:>7.4f}  "
            f"{row['n_luld_lower']:>7}  "
            f"{row['n_luld_upper']:>7}  "
            f"{row['n_exit_d']:>7}  "
            f"{row['n_epg_window_close']:>7}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase E sweep runner")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    main(dry_run=args.dry_run)
