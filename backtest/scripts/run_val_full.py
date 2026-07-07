"""
Phase VAL-FULL T2 — run the LOCKED val_r4 p=0.80 config against the full held-out pool.

Locked config (do NOT sweep / modify):
  entry_mode      = first_pass
  p_open = p_close = 0.80
  max_entry_lag_sec = 500   (entry deadline; NOT the exit time gate)
  t_gate_sec      = None     (NO time-gate exit)
  LULD / EXIT_D   = off (rapid-mode defaults)
  exit stack      = epg_window_close -> session_end only

Same runner and same flags as run_r1_final_sweep.py's sym_p80 arm, only the
event file changes (val_full.json instead of val_r4_stratified.json).
"""
import subprocess
import sys
import time
from pathlib import Path

PYTHON = r"D:\Trading Research\.venv\Scripts\python.exe"
BASE = Path(__file__).resolve().parent.parent.parent  # scanner-epg-momentum/

EVENT_FILE = str(BASE / "backtest" / "data" / "val_full.json")
RESULTS_DIR = BASE / "backtest" / "results" / "phase_val_full" / "full_pool_run"

cmd = [
    PYTHON, "-m", "backtest.runner_rapid",
    "--entry-mode", "first_pass",
    "--event-file", EVENT_FILE,
    "--split", "val",
    "--max-entry-lag-sec", "500",
    "--p-open", "0.80",
    "--p-close", "0.80",
    "--results-dir", str(RESULTS_DIR),
]

print("VAL-FULL T2 — locked p=0.80 config over full pool")
print("  event file:", EVENT_FILE)
print("  results:   ", RESULTS_DIR)
print("  cmd:", " ".join(cmd), flush=True)

t0 = time.time()
result = subprocess.run(cmd, cwd=str(BASE))
elapsed = time.time() - t0
print(f"\nrunner exit={result.returncode} in {elapsed:.0f}s")
sys.exit(result.returncode)
