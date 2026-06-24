"""
R1 T1 — Symmetric EPG gate threshold sweep (sequential).

Spec: p_open = p_close in {0.50, 0.55, 0.60, 0.65, 0.70, 0.75}. Six configs.

Root cause of original failure:
  - 30 processes launched in parallel → OOM → workers killed
  - 20/30 combinations had p_close > p_open → ValueError in gate constructor
Fix: run T1 (symmetric only, valid by construction) sequentially, one at a time.
"""
import subprocess
import sys
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/

EVENT_FILE = r"D:\Trading Research\data\val_mdr150_diagnostic.json"
RESULTS_BASE = BASE / "backtest" / "results" / "phase_r1_mdr150"

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

print(f"R1 T1 symmetric sweep: {len(THRESHOLDS)} configs (p_open = p_close)")
print(f"Thresholds: {THRESHOLDS}")
print(f"Results dir: {RESULTS_BASE}")
print()

for idx, p in enumerate(THRESHOLDS, 1):
    tag = f"sym_p{int(p*100)}"
    results_dir = RESULTS_BASE / tag
    print(f"[{idx}/{len(THRESHOLDS)}] p={p:.2f} -> {tag} ...", flush=True)
    t0 = time.time()

    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "first_pass",
        "--event-file", EVENT_FILE,
        "--split", "val",
        "--max-entry-lag-sec", "300",
        "--p-open", str(p),
        "--p-close", str(p),
        "--results-dir", str(results_dir),
    ]

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        cwd=str(BASE),
    )

    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  ERROR (exit={result.returncode}) in {elapsed:.0f}s")
        print(f"  STDERR tail:\n{result.stderr[-800:]}")
    else:
        summary_line = ""
        for line in result.stderr.splitlines():
            if "Summary:" in line:
                summary_line = line.split("Summary: ")[1]
                break
        if summary_line:
            print(f"  OK ({elapsed:.0f}s): {summary_line}")
        else:
            print(f"  OK ({elapsed:.0f}s): (no Summary line in stderr)")
            # Print last few log lines for context
            lines = result.stderr.splitlines()
            for line in lines[-5:]:
                print(f"    {line}")

print()
print("R1 T1 symmetric sweep complete.")
