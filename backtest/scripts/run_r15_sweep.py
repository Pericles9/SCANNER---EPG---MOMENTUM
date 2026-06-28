"""
Phase R1.5 — time-gate sweep. T_gate in {400, 500, 600} at fixed p_open=p_close=0.65,
max_entry_lag_sec=300, entry_mode=first_pass, val MDR>=150 (n=100, seed=42).

Writes runner output to phase_r15/t_gate_{N}/. Aggregation/joins done by aggregate_r15.py.
Baseline phase_r1_fixed/ is NOT touched.
"""
import subprocess
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent
EVENT_FILE = r"D:\Trading Research\data\val_mdr150_diagnostic.json"
RESULTS = BASE / "backtest" / "results" / "phase_r15"

T_GATES = [400, 500, 600]

print(f"R1.5 time-gate sweep: T_gate in {T_GATES} at p=0.65")
for idx, tg in enumerate(T_GATES, 1):
    out = RESULTS / f"t_gate_{tg}"
    print(f"[{idx}/{len(T_GATES)}] T_gate={tg} -> {out.name} ...", flush=True)
    t0 = time.time()
    cmd = [
        PYTHON, "-m", "backtest.runner_rapid",
        "--entry-mode", "first_pass",
        "--event-file", EVENT_FILE,
        "--split", "val",
        "--max-entry-lag-sec", "300",
        "--p-open", "0.65", "--p-close", "0.65",
        "--t-gate-sec", str(tg),
        "--results-dir", str(out),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(BASE))
    el = time.time() - t0
    if r.returncode != 0:
        print(f"  ERROR ({el:.0f}s): {r.stderr[-1200:]}")
    else:
        line = ""
        for ln in r.stderr.splitlines():
            if "Summary:" in ln:
                line = ln.split("Summary: ")[1]
                break
        print(f"  OK ({el:.0f}s): {line}")
print("R1.5 sweep complete.")
