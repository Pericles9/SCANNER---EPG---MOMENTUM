"""
Phase SEB-X CLI orchestrator.

Runs Tasks 1-4 in order with gate enforcement.

Usage:
  python backtest/tools/seb_x/run_seb_x.py [--skip-paths] [--skip-diag]

Flags:
  --skip-paths    Skip Task 1 (build_paths) if paths.parquet already exists.
  --skip-diag     Skip Task 2 (diagnostics); just show sweep + report.
  --skip-sweep    Skip Task 3 (sweep) if sweep.parquet already exists.

All outputs go to backtest/results/seb_x/.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_ROOT.parent
for _p in (str(_PROJECT_ROOT), str(_REPO_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x"
PATHS_PARQUET = OUTPUT_DIR / "paths.parquet"
SWEEP_PARQUET = OUTPUT_DIR / "sweep.parquet"
SWEEP_AGG     = OUTPUT_DIR / "sweep_agg.parquet"


def _elapsed(t0: float) -> str:
    s = time.time() - t0
    return "%dm%ds" % (int(s) // 60, int(s) % 60)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("seb_x")

    ap = argparse.ArgumentParser(description="Phase SEB-X orchestrator")
    ap.add_argument("--skip-paths",  action="store_true", help="Skip build_paths if paths.parquet exists")
    ap.add_argument("--skip-diag",   action="store_true", help="Skip diagnostics")
    ap.add_argument("--skip-sweep",  action="store_true", help="Skip sweep if sweep.parquet exists")
    args = ap.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    t0_total = time.time()

    # -- Task 1: Build paths --
    if args.skip_paths and PATHS_PARQUET.exists():
        log.info("[Task 1] SKIP -- %s exists", PATHS_PARQUET)
    else:
        log.info("[Task 1] Building forward paths (this reads ~990 parquets; ~5-10 min)...")
        t0 = time.time()
        from tools.seb_x.build_paths import main as build_main
        build_main()
        log.info("[Task 1] done in %s", _elapsed(t0))

    # Gate check
    if not PATHS_PARQUET.exists():
        log.error("[Task 1] FAILED -- paths.parquet not written. STOP.")
        sys.exit(1)

    # -- Task 2: Diagnostics --
    if args.skip_diag:
        log.info("[Task 2] SKIP (--skip-diag)")
    else:
        log.info("[Task 2] Running path diagnostics (Gate B)...")
        t0 = time.time()
        from tools.seb_x.diagnostics import main as diag_main
        diag_main()
        log.info("[Task 2] done in %s", _elapsed(t0))

    # -- Task 3: Sweep --
    if args.skip_sweep and SWEEP_PARQUET.exists() and SWEEP_AGG.exists():
        log.info("[Task 3] SKIP -- sweep.parquet exists")
    else:
        log.info("[Task 3] Running exit rule sweep...")
        t0 = time.time()
        from tools.seb_x.sweep import main as sweep_main
        sweep_main()
        log.info("[Task 3] done in %s", _elapsed(t0))

    if not SWEEP_PARQUET.exists():
        log.error("[Task 3] FAILED -- sweep.parquet not written. STOP.")
        sys.exit(1)

    # -- Task 4: Report + Gate C --
    log.info("[Task 4] Generating report and Gate C validation...")
    t0 = time.time()
    from tools.seb_x.report import main as report_main
    report_main()
    log.info("[Task 4] done in %s", _elapsed(t0))

    log.info("Phase SEB-X complete in %s. Outputs in %s", _elapsed(t0_total), OUTPUT_DIR)
    print("")
    print("Phase SEB-X complete in %s" % _elapsed(t0_total))
    print("Outputs:")
    for f in sorted(OUTPUT_DIR.glob("*.parquet")) + sorted(OUTPUT_DIR.glob("*.txt")) + sorted(OUTPUT_DIR.glob("*.md")):
        size_kb = f.stat().st_size // 1024
        print("  %-40s  %d KB" % (f.name, size_kb))


if __name__ == "__main__":
    main()
