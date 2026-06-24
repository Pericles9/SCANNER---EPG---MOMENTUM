"""
Phase SEB-X v2 -- CLI orchestrator.

Tasks:
  1 (sigma):  Compute Parkinson sigma + ADR floor + sigma_vwap per entry.
              Gate A: MFE reuse integrity + sigma non-degenerate.
  3 (sweep):  Sweep 244 configs x 2 sigma units against frozen paths.
  4 (report): Complexity ladder + Gate C (vol-regime, live PASS/FAIL) + Gate D (edge-decay).

Skip flags:
  --skip-sigma   (requires sigma_context.parquet to already exist)
  --skip-sweep   (requires sweep.parquet to already exist)

Usage:
  python backtest/tools/seb_x_v2/run_seb_x_v2.py
  python backtest/tools/seb_x_v2/run_seb_x_v2.py --skip-sigma
  python backtest/tools/seb_x_v2/run_seb_x_v2.py --skip-sigma --skip-sweep
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT    = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_ROOT.parent
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tools.seb_x_v2.sigma_v2  import main as run_sigma
from tools.seb_x_v2.sweep_v2  import main as run_sweep
from tools.seb_x_v2.report_v2 import main as run_report

log = logging.getLogger("seb_x_v2")

OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x_v2"
SIGMA_PARQUET = OUTPUT_DIR / "sigma_context.parquet"
SWEEP_PARQUET = OUTPUT_DIR / "sweep.parquet"
SWEEP_AGG     = OUTPUT_DIR / "sweep_agg.parquet"
REPORT_PATH   = OUTPUT_DIR / "exit_report_v2.md"


def _elapsed(t0: float) -> str:
    dt = int(time.time() - t0)
    return "%dm%ds" % (dt // 60, dt % 60)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Phase SEB-X v2 run")
    parser.add_argument("--skip-sigma",  action="store_true", help="Skip sigma computation (Task 1)")
    parser.add_argument("--skip-sweep",  action="store_true", help="Skip exit rule sweep (Task 3)")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t_total = time.time()

    # ------------------------------------------------------------------
    # Task 1: sigma v2 context
    # ------------------------------------------------------------------
    if args.skip_sigma:
        if not SIGMA_PARQUET.exists():
            log.error("--skip-sigma set but sigma_context.parquet not found at %s", SIGMA_PARQUET)
            sys.exit(1)
        log.info("[Task 1] Skipping sigma computation (sigma_context.parquet exists)")
    else:
        log.info("[Task 1] Computing sigma v2 context (Parkinson + ADR floor + sigma_vwap)...")
        t0 = time.time()
        run_sigma()
        log.info("[Task 1] done in %s", _elapsed(t0))

    # ------------------------------------------------------------------
    # Task 3: sweep
    # ------------------------------------------------------------------
    if args.skip_sweep:
        if not SWEEP_PARQUET.exists():
            log.error("--skip-sweep set but sweep.parquet not found at %s", SWEEP_PARQUET)
            sys.exit(1)
        log.info("[Task 3] Skipping sweep (sweep.parquet exists)")
    else:
        log.info("[Task 3] Running exit rule sweep (244 configs x 2 sigma units)...")
        t0 = time.time()
        run_sweep()
        log.info("[Task 3] done in %s", _elapsed(t0))

    # ------------------------------------------------------------------
    # Task 4: report
    # ------------------------------------------------------------------
    log.info("[Task 4] Generating report (Gate C + Gate D)...")
    t0 = time.time()
    run_report()
    log.info("[Task 4] done in %s", _elapsed(t0))

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    log.info("Phase SEB-X v2 complete in %s. Outputs in %s", _elapsed(t_total), OUTPUT_DIR)

    print("")
    print("Phase SEB-X v2 complete in %s" % _elapsed(t_total))
    print("Outputs:")
    artifacts = [
        SIGMA_PARQUET,
        SWEEP_PARQUET,
        SWEEP_AGG,
        REPORT_PATH,
    ]
    for p in artifacts:
        if p.exists():
            kb = p.stat().st_size // 1024
            print("  %-40s %d KB" % (p.name, kb))
        else:
            print("  %-40s (not found)" % p.name)


if __name__ == "__main__":
    main()
