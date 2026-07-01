"""
Phase REBUILD-VAL T4 — R1 gate threshold sweep on val_r4_stratified.json.

Sweep: p_open = p_close in {0.50, 0.55, 0.60, 0.65, 0.70, 0.75}
T_gate: max_entry_lag_sec=500 (option A, Cooper confirmed 2026-06-30)
Sample: val_r4_stratified.json (mom_pct tercile strata, 30/40/30, n=100)
Results: backtest/results/phase_r1_final/
"""
import subprocess
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/

EVENT_FILE = str(BASE / "backtest" / "data" / "val_r4_stratified.json")
RESULTS_BASE = BASE / "backtest" / "results" / "phase_r1_final"

THRESHOLDS = [0.80, 0.85, 0.90, 0.95]
MAX_ENTRY_LAG = 500  # option A

print(f"REBUILD-VAL T4 — R1 symmetric sweep on val_r4_stratified.json")
print(f"  Thresholds: {THRESHOLDS}")
print(f"  max_entry_lag_sec: {MAX_ENTRY_LAG}")
print(f"  Results dir: {RESULTS_BASE}")
print()

for idx, p in enumerate(THRESHOLDS, 1):
    tag = f"sym_p{int(p * 100)}"
    results_dir = RESULTS_BASE / tag
    print(f"[{idx}/{len(THRESHOLDS)}] p={p:.2f} -> {tag} ...", flush=True)
    t0 = time.time()

    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "first_pass",
        "--event-file", EVENT_FILE,
        "--split", "val",
        "--max-entry-lag-sec", str(MAX_ENTRY_LAG),
        "--p-open", str(p),
        "--p-close", str(p),
        "--results-dir", str(results_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE))

    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  ERROR (exit={result.returncode}) in {elapsed:.0f}s")
        print(f"  STDERR tail:\n{result.stderr[-1200:]}")
    else:
        summary_line = ""
        for line in result.stderr.splitlines():
            if "Summary:" in line:
                summary_line = line.split("Summary: ")[1]
                break
        if summary_line:
            print(f"  OK ({elapsed:.0f}s): {summary_line}")
        else:
            print(f"  OK ({elapsed:.0f}s): (no Summary line)")
            for line in result.stderr.splitlines()[-5:]:
                print(f"    {line}")

print()
print("T4 sweep complete.")
