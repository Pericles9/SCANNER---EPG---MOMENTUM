"""
Phase SEB-X Task 2 -- Path diagnostics and sweep-center calibration.

Reads paths.parquet (990 entries with forward bar paths) and computes
the distributions needed to set sweep centers for each exit rule.

Gate B: each rule's sweep center must be justified by a distribution here.
        This file prints all Gate B values explicitly.

Outputs:
  results/seb_x/diag_report.txt  -- human-readable Gate B summary
  results/seb_x/diag_data.parquet -- per-entry diagnostic values (for charts)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT_DIR = _REPO_ROOT / "results" / "seb_x"
PATHS_PARQUET = OUTPUT_DIR / "paths.parquet"
DIAG_REPORT = OUTPUT_DIR / "diag_report.txt"
DIAG_DATA_PARQUET = OUTPUT_DIR / "diag_data.parquet"


def _percentiles(arr: np.ndarray, qs=(10, 25, 50, 75, 90, 95)) -> dict:
    """Return dict of {pN: value} for the given quantiles."""
    a = arr[~np.isnan(arr)]
    if len(a) == 0:
        return {f"p{q}": float("nan") for q in qs}
    return {f"p{q}": float(np.percentile(a, q)) for q in qs}


def _fmt_pct_table(label: str, percs: dict) -> str:
    keys = sorted(percs.keys(), key=lambda k: int(k[1:]))
    header = "  " + "  ".join(f"{k:>6}" for k in keys)
    row    = "  " + "  ".join(f"{percs[k]*100:>6.2f}" for k in keys)
    return f"{label}\n{header}\n{row}"


def _fmt_raw_table(label: str, percs: dict) -> str:
    keys = sorted(percs.keys(), key=lambda k: int(k[1:]))
    header = "  " + "  ".join(f"{k:>6}" for k in keys)
    row    = "  " + "  ".join(f"{percs[k]:>6.4f}" for k in keys)
    return f"{label}\n{header}\n{row}"


def run_diagnostics(df: pd.DataFrame) -> tuple[dict, pd.DataFrame]:
    """Compute diagnostics from paths DataFrame. Return (summary_dict, per_entry_df)."""
    n = len(df)
    runners  = df[df["is_runner_rc"]].copy()
    losers   = df[~df["is_runner_rc"]].copy()
    n_run    = len(runners)
    n_loss   = len(losers)

    # Sigma stats
    sigma_all = df["sigma"].values
    sigma_q   = _percentiles(sigma_all)
    low_vol_thresh  = float(np.percentile(sigma_all[sigma_all > 0], 50))

    diag_rows: list[dict] = []

    # Per-entry derived values
    for _, row in df.iterrows():
        n_path = int(row["path_n_bars"])
        if n_path == 0:
            diag_rows.append({
                "ticker": row["ticker"], "date": row["date"],
                "sigma": row["sigma"], "is_runner_rc": row["is_runner_rc"],
                "mae_pre_mfe_sigma": float("nan"),
                "time_to_mfe_30m": row["time_to_mfe_30m"],
                "giveback_30to45_sigma": float("nan"),
                "early_sigma_bar1": float("nan"),
                "early_sigma_bar3": float("nan"),
                "early_sigma_bar5": float("nan"),
                "eod_sigma": float("nan"),
            })
            continue

        sigma = float(row["sigma"])
        ep    = float(row["entry_price"])
        ph    = np.array(row["path_high"],  dtype=np.float64)
        pl    = np.array(row["path_low"],   dtype=np.float64)
        pc    = np.array(row["path_close"], dtype=np.float64)

        mae_pre = float(row["mae_pre_mfe_30m"])
        mae_pre_sigma = (abs(mae_pre) / sigma) if (sigma > 0 and not np.isnan(mae_pre)) else float("nan")

        # Give-back from 30m peak to 45m low (runners only)
        n_30 = min(30, n_path)
        n_45 = min(45, n_path)
        if row["is_runner_rc"] and n_45 > n_30 and n_30 > 0:
            peak_30 = float(np.max(ph[:n_30]))
            low_30_45 = float(np.min(pl[n_30:n_45]))
            giveback = (peak_30 - low_30_45) / sigma if sigma > 0 else float("nan")
        else:
            giveback = float("nan")

        # Early bar direction (close vs entry)
        def _bar_sigma(bar_idx: int) -> float:
            if bar_idx >= n_path or sigma <= 0:
                return float("nan")
            return (float(pc[bar_idx]) - ep) / sigma

        diag_rows.append({
            "ticker":              row["ticker"],
            "date":                row["date"],
            "sigma":               sigma,
            "is_runner_rc":        bool(row["is_runner_rc"]),
            "mae_pre_mfe_sigma":   mae_pre_sigma,
            "time_to_mfe_30m":     int(row["time_to_mfe_30m"]),
            "giveback_30to45_sigma": giveback,
            "early_sigma_bar1":    _bar_sigma(0),
            "early_sigma_bar3":    _bar_sigma(2),
            "early_sigma_bar5":    _bar_sigma(4),
            "eod_sigma":           (float(pc[-1]) - ep) / sigma if sigma > 0 else float("nan"),
        })

    diag_df = pd.DataFrame(diag_rows)

    # -- Gate B distributions --

    # R1 center: p90-p95 of winners' pre-MFE MAE (in sigma)
    run_mae_sig = diag_df[diag_df["is_runner_rc"]]["mae_pre_mfe_sigma"].values
    r1_center   = _percentiles(run_mae_sig, qs=(50, 75, 90, 95))

    # R2 center: time-to-MFE distribution for winners
    run_ttm = diag_df[diag_df["is_runner_rc"]]["time_to_mfe_30m"].values.astype(float)
    run_ttm = run_ttm[run_ttm > 0]
    r2_center = _percentiles(run_ttm, qs=(10, 25, 50, 75, 90))

    # Time-to-MFE for losers (need to decide T_dead independent of winner distribution)
    loss_ttm = diag_df[~diag_df["is_runner_rc"]]["time_to_mfe_30m"].values.astype(float)
    loss_ttm = loss_ttm[loss_ttm > 0]
    r2_loss   = _percentiles(loss_ttm, qs=(10, 25, 50, 75, 90))

    # R3 center: give-back from 30m peak to 45m low (runners only)
    run_gb = diag_df["giveback_30to45_sigma"].values
    r3_center = _percentiles(run_gb, qs=(25, 50, 75, 90, 95))

    # Early sigma at bar1/3/5 for winners vs losers
    early_run  = diag_df[diag_df["is_runner_rc"]]["early_sigma_bar1"].values
    early_loss = diag_df[~diag_df["is_runner_rc"]]["early_sigma_bar1"].values
    early_run_p  = _percentiles(early_run)
    early_loss_p = _percentiles(early_loss)

    summary = {
        "n_total":        n,
        "n_runners":      n_run,
        "n_losers":       n_loss,
        "runner_rate":    n_run / max(n, 1),
        "sigma_q":        sigma_q,
        "low_vol_thresh": low_vol_thresh,
        "r1_center":      r1_center,    # winners' pre-MFE MAE in sigma: Gate B for R1
        "r2_winners_ttm": r2_center,    # time-to-MFE winners: Gate B for T_dead
        "r2_losers_ttm":  r2_loss,      # time-to-MFE losers
        "r3_giveback":    r3_center,    # give-back from peak: Gate B for R3
        "early_run_bar1": early_run_p,
        "early_loss_bar1": early_loss_p,
    }
    return summary, diag_df


def format_report(s: dict) -> str:
    lines = [
        "Phase SEB-X -- Path Diagnostics (Gate B)",
        "=" * 60,
        "",
        "Universe",
        f"  total entries:   {s['n_total']}",
        f"  runners (MFE30>=5%): {s['n_runners']} ({s['runner_rate']*100:.1f}%)",
        f"  losers:          {s['n_losers']}",
        "",
        "Per-event sigma (primary ATR, in price units)",
        _fmt_raw_table("  sigma percentiles:", s["sigma_q"]),
        f"  vol-regime split threshold (median): {s['low_vol_thresh']:.5f}",
        "",
        "--- Gate B: R1 sweep center ---",
        "Winners' pre-MFE MAE in sigma-units (how deep winners dip before peak)",
        _fmt_raw_table("  (p50=tolerate, p90-p95=hard floor center):", s["r1_center"]),
        "  => R1 k1 sweep center: p90-p95 of this distribution",
        "     Keep k1 ABOVE p90 to avoid cutting 90% of winners early.",
        "",
        "--- Gate B: R2 sweep center ---",
        "Time-to-MFE(30m) for WINNERS (bars from entry to peak)",
        _fmt_raw_table("  winners:", s["r2_winners_ttm"]),
        "Time-to-MFE(30m) for LOSERS",
        _fmt_raw_table("  losers:", s["r2_losers_ttm"]),
        "  => T_dead should be < p50 of winners' time-to-MFE (don't kill running winners).",
        "     Center sweep on p25-p50 of losers' time where they haven't moved.",
        "",
        "--- Gate B: R3 sweep center ---",
        "Give-back from 30m peak to 45m low (runners only, in sigma-units)",
        _fmt_raw_table("  (p25=tight trail, p75=loose trail):", s["r3_giveback"]),
        "  => R3 g (give-back) sweep center: p25-p50 of this distribution.",
        "     Tighter captures more, but whips winners prematurely.",
        "",
        "--- Early direction (sigma-units at bar 1 from entry) ---",
        _fmt_raw_table("  Winners bar-1 close vs entry:", s["early_run_bar1"]),
        _fmt_raw_table("  Losers  bar-1 close vs entry:", s["early_loss_bar1"]),
        "  => Winners establish +sigma direction faster; losers drift flat/negative.",
        "",
    ]
    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    if not PATHS_PARQUET.exists():
        log.error("paths.parquet not found -- run build_paths.py first")
        sys.exit(1)

    log.info("Loading %s", PATHS_PARQUET)
    df = pd.read_parquet(str(PATHS_PARQUET))
    log.info("  %d entries loaded", len(df))

    summary, diag_df = run_diagnostics(df)

    report_txt = format_report(summary)
    DIAG_REPORT.write_text(report_txt, encoding="utf-8")
    log.info("Wrote %s", DIAG_REPORT)

    diag_df.to_parquet(str(DIAG_DATA_PARQUET), index=False)
    log.info("Wrote %s", DIAG_DATA_PARQUET)

    print("")
    print(report_txt)

    return summary


if __name__ == "__main__":
    main()
