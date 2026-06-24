"""
Phase SEB-X v2-VIZ -- CLI orchestrator.

Tasks:
  1 (trades):   Per-trade exits for visualization stacks -> per_trade_exits.parquet
  2 (metrics):  Distribution metrics -> metrics_v2viz.md + .csv
  3 (charts):   PnL histograms, box plots, equity curve -> *.png
  4 (candles):  Candlestick charts for curated trade set -> trades/*.png + contact sheets

Skip flags:
  --skip-trades   (requires per_trade_exits.parquet)
  --skip-metrics  (requires per_trade_exits.parquet for charts; metrics for report)
  --skip-charts   skip Task 3 distribution charts
  --skip-candles  skip Task 4 candlestick charts

Ad-hoc selector:
  --ticker TICKER --date YYYY-MM-DD [--stack LABEL]
  Renders a single trade chart without running the full pipeline.

Usage:
  python backtest/tools/seb_x_v2viz/run_v2viz.py
  python backtest/tools/seb_x_v2viz/run_v2viz.py --skip-trades --skip-metrics
  python backtest/tools/seb_x_v2viz/run_v2viz.py --ticker AAPL --date 2023-03-15
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

from tools.seb_x_v2viz.per_trade     import main as run_trades
from tools.seb_x_v2viz.metrics       import main as run_metrics
from tools.seb_x_v2viz.distrib_charts import main as run_charts
from tools.seb_x_v2viz.candle_charts  import main as run_candles, render_one

log = logging.getLogger("seb_x_v2viz")

OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x_v2viz"
TRADES_FILE   = OUTPUT_DIR / "per_trade_exits.parquet"
METRICS_MD    = OUTPUT_DIR / "metrics_v2viz.md"
METRICS_CSV   = OUTPUT_DIR / "metrics_v2viz.csv"


def _elapsed(t0: float) -> str:
    dt = int(time.time() - t0)
    return f"{dt // 60}m{dt % 60}s"


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    parser = argparse.ArgumentParser(description="Phase SEB-X v2-VIZ")
    parser.add_argument("--skip-trades",  action="store_true")
    parser.add_argument("--skip-metrics", action="store_true")
    parser.add_argument("--skip-charts",  action="store_true")
    parser.add_argument("--skip-candles", action="store_true")
    parser.add_argument("--ticker", default=None, help="Ad-hoc: chart a single trade")
    parser.add_argument("--date",   default=None, help="Ad-hoc: chart a single trade")
    parser.add_argument("--stack",  default="B0+R1+R3_vwap",
                        help="Stack label for --ticker/--date (default: B0+R1+R3_vwap)")
    args = parser.parse_args()

    # Ad-hoc single-trade chart
    if args.ticker and args.date:
        render_one(args.ticker, args.date, args.stack)
        return

    t_total = time.time()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.skip_trades:
        if not TRADES_FILE.exists():
            log.error("--skip-trades set but per_trade_exits.parquet not found")
            sys.exit(1)
        log.info("[Task 1] Skipped (per_trade_exits.parquet exists)")
    else:
        log.info("[Task 1] Per-trade exits ...")
        t0 = time.time()
        run_trades()
        log.info("[Task 1] done in %s", _elapsed(t0))

    if args.skip_metrics:
        log.info("[Task 2] Skipped")
    else:
        log.info("[Task 2] Distribution metrics ...")
        t0 = time.time()
        run_metrics()
        log.info("[Task 2] done in %s", _elapsed(t0))

    if args.skip_charts:
        log.info("[Task 3] Skipped")
    else:
        log.info("[Task 3] PnL distribution charts ...")
        t0 = time.time()
        run_charts()
        log.info("[Task 3] done in %s", _elapsed(t0))

    if args.skip_candles:
        log.info("[Task 4] Skipped")
    else:
        log.info("[Task 4] Candlestick charts ...")
        t0 = time.time()
        run_candles()
        log.info("[Task 4] done in %s", _elapsed(t0))

    log.info("Phase SEB-X v2-VIZ complete in %s", _elapsed(t_total))

    print(f"\nPhase SEB-X v2-VIZ complete in {_elapsed(t_total)}")
    print("Outputs:")
    artifacts = [
        TRADES_FILE,
        METRICS_MD,
        METRICS_CSV,
    ]
    for p in artifacts:
        if p.exists():
            kb = p.stat().st_size // 1024
            print(f"  {p.name:<45} {kb} KB")
        else:
            print(f"  {p.name:<45} (not found)")

    pngs = list(OUTPUT_DIR.glob("*.png")) + list((OUTPUT_DIR / "trades").glob("*.png"))
    print(f"  PNGs: {len(pngs)} files in results/seb_x_v2viz/")


if __name__ == "__main__":
    main()
