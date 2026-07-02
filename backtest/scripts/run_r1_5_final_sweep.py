"""
Phase R1.5-Final — T_gate (time-gate exit) resweep on val_r4_stratified.json.

Sweeps the R1.5 single-shot time-gate exit (--t-gate-sec) at fixed p_open=p_close=0.80,
max_entry_lag_sec=500 (R1-Final entry setting held constant), entry_mode=first_pass.
Isolates the exit-side time gate; entry side is identical to R1-Final.

Exit stack: time_gate -> epg_window_close -> session_end. LULD retired (not in rapid runner).
Sample: val_r4_stratified.json (mom_pct tercile strata, 30/40/30, n=100, seed=42).
Results: backtest/results/phase_r1_5_final/tg{N}/
"""
import subprocess
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/

EVENT_FILE = str(BASE / "backtest" / "data" / "val_r4_stratified.json")
RESULTS_BASE = BASE / "backtest" / "results" / "phase_r1_5_final"

T_GATES = [300, 400, 500, 600, 750]
P = 0.80
MAX_ENTRY_LAG = 500

print("Phase R1.5-Final — time-gate exit resweep on val_r4_stratified.json")
print(f"  T_gate values: {T_GATES}")
print(f"  p_open=p_close={P}, max_entry_lag_sec={MAX_ENTRY_LAG}, entry_mode=first_pass")
print(f"  Results dir: {RESULTS_BASE}")
print()

for idx, tg in enumerate(T_GATES, 1):
    tag = f"tg{tg}"
    results_dir = RESULTS_BASE / tag
    print(f"[{idx}/{len(T_GATES)}] T_gate={tg}s -> {tag} ...", flush=True)
    t0 = time.time()

    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "first_pass",
        "--event-file", EVENT_FILE,
        "--split", "val",
        "--max-entry-lag-sec", str(MAX_ENTRY_LAG),
        "--t-gate-sec", str(tg),
        "--p-open", str(P),
        "--p-close", str(P),
        "--results-dir", str(results_dir),
    ]

    result = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE))
    elapsed = time.time() - t0
    if result.returncode != 0:
        print(f"  ERROR (exit={result.returncode}) in {elapsed:.0f}s")
        print(f"  STDERR tail:\n{result.stderr[-1500:]}")
    else:
        summary_line = ""
        for line in result.stderr.splitlines():
            if "Summary:" in line:
                summary_line = line.split("Summary: ")[1]
                break
        print(f"  OK ({elapsed:.0f}s): {summary_line or '(no Summary line)'}")

print()
print("R1.5-Final sweep complete.")
