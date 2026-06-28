"""
FIX-T4E-T6B / R1-FIXED T1 — Symmetric EPG gate threshold sweep (sequential).

Re-run of the R1 T1 symmetric sweep against the T4e+T6b-fixed runner.
Config identical to the original phase_r1_mdr150 sym sweep
(entry_mode=first_pass, max_entry_lag_sec=300, p_open=p_close, seed=42,
val sample = val_mdr150_diagnostic.json, 100 events) — only the output
directory differs (phase_r1_fixed/ instead of phase_r1_mdr150/).

Old results in phase_r1/ and phase_r1_mdr150/ are NOT touched.
"""
import subprocess
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/

EVENT_FILE = r"D:\Trading Research\data\val_mdr150_diagnostic.json"
RESULTS_BASE = BASE / "backtest" / "results" / "phase_r1_fixed"

THRESHOLDS = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]

print(f"R1-FIXED T1 symmetric sweep: {len(THRESHOLDS)} configs (p_open = p_close)")
print(f"Thresholds: {THRESHOLDS}")
print(f"Results dir: {RESULTS_BASE}")
print(f"Runner: fixed (T4e + T6b, commit 724617a)")
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
print("R1-FIXED T1 symmetric sweep complete.")
