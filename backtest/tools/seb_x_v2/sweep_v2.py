"""
Phase SEB-X v2 Task 3 -- Exit rule sweep under sigma_primary and sigma_vwap.

Reuses v1 forward paths (paths.parquet) and policy configs.
Merges sigma_context.parquet for per-entry sigma values.
Runs every policy config twice: once with sigma_final, once with sigma_vwap.
Adds sigma_unit column to all per-entry rows.

Policy families and priority are unchanged from v1 sweep.py.

Outputs:
  results/seb_x_v2/sweep.parquet      (per-entry x config x sigma_unit)
  results/seb_x_v2/sweep_agg.parquet  (aggregated per config x sigma_unit)
"""
from __future__ import annotations

import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT.parent))
sys.path.insert(0, str(_REPO_ROOT))

PATHS_V1_PARQUET = _REPO_ROOT / "results" / "seb_x"    / "paths.parquet"
SIGMA_PARQUET    = _REPO_ROOT / "results" / "seb_x_v2" / "sigma_context.parquet"
OUTPUT_DIR       = _REPO_ROOT / "results" / "seb_x_v2"
SWEEP_PARQUET    = OUTPUT_DIR / "sweep.parquet"
SWEEP_AGG        = OUTPUT_DIR / "sweep_agg.parquet"

from tools.seb_x.sweep import (   # noqa: E402
    _simulate_one,
    _metrics_for_exits,
    build_policy_configs,
)


def run_sweep_v2(
    df_paths: pd.DataFrame,
    df_sigma: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Sweep all policy configs under sigma_primary and sigma_vwap.

    Returns (per_entry_df, agg_df).
    Each output row includes a 'sigma_unit' column ("primary" | "vwap").
    """
    df = pd.merge(
        df_paths,
        df_sigma[["ticker", "date", "sigma_final", "sigma_vwap", "sigma_source",
                  "sigma_primary", "sigma_robust", "adr"]],
        on=["ticker", "date"],
        how="inner",
    )
    if len(df) != len(df_paths):
        log.warning(
            "Merge dropped %d rows (paths=%d, sigma=%d, merged=%d)",
            len(df_paths) - len(df), len(df_paths), len(df_sigma), len(df),
        )

    n = len(df)
    all_ph = [np.array(r, dtype=np.float64) for r in df["path_high"]]
    all_pl = [np.array(r, dtype=np.float64) for r in df["path_low"]]
    all_pc = [np.array(r, dtype=np.float64) for r in df["path_close"]]
    all_pv = [np.array(r, dtype=np.float64) for r in df["path_vwap"]]

    entry_prices   = df["entry_price"].values.astype(np.float64)
    mfe_30m        = df["mfe_30m_rc"].values.astype(np.float64)
    mfe_45m        = df["mfe_45m_rc"].values.astype(np.float64)
    tickers        = df["ticker"].values
    dates          = df["date"].values
    is_runner      = df["is_runner_rc"].values.astype(bool)
    session_bucket = df["session_bucket"].values

    sigma_primary_vals = df["sigma_final"].values.astype(np.float64)   # = max(parkinson, floor)
    sigma_vwap_vals    = df["sigma_vwap"].values.astype(np.float64)

    configs   = build_policy_configs()
    n_configs = len(configs)
    log.info("Sweeping %d configs x %d entries x 2 sigma units", n_configs, n)

    per_entry_rows: list[dict] = []
    agg_rows:       list[dict] = []

    for sigma_unit in ("primary", "vwap"):
        sigmas = sigma_primary_vals if sigma_unit == "primary" else sigma_vwap_vals

        for ci, cfg in enumerate(configs):
            if ci % 50 == 0:
                log.info("  sigma_unit=%s  config %d / %d  (%s)",
                         sigma_unit, ci, n_configs, cfg["policy"])

            exit_prices_all  = np.empty(n, dtype=np.float64)
            exit_bar_all     = np.empty(n, dtype=np.int64)
            exit_reason_all  = [""] * n

            for i in range(n):
                bar, price, reason = _simulate_one(
                    all_ph[i], all_pl[i], all_pc[i], all_pv[i],
                    entry_prices[i], sigmas[i],
                    use_b0   = cfg.get("use_b0",   False),
                    use_r1   = cfg.get("use_r1",   False),  k1       = cfg.get("k1",       2.0),
                    use_r2   = cfg.get("use_r2",   False),  t_dead   = cfg.get("t_dead",   15),
                    a        = cfg.get("a",         0.0),
                    use_r3   = cfg.get("use_r3",   False),  arm_mult = cfg.get("arm_mult", 2.0),
                    g        = cfg.get("g",         1.0),
                    use_tpsl = cfg.get("use_tpsl", False),  k_tp     = cfg.get("k_tp",     3.0),
                    k_sl     = cfg.get("k_sl",      1.5),
                )
                exit_prices_all[i] = price
                exit_bar_all[i]    = bar
                exit_reason_all[i] = reason

            metrics = _metrics_for_exits(
                exit_prices_all, entry_prices, sigmas, mfe_30m, mfe_45m
            )

            # Aggregate row
            agg_row = {**cfg, **metrics, "sigma_unit": sigma_unit}
            for k in ("k1", "t_dead", "a", "arm_mult", "g", "k_tp", "k_sl"):
                agg_row.setdefault(k, float("nan"))
            for k in ("use_b0", "use_r1", "use_r2", "use_r3", "use_tpsl"):
                agg_row.setdefault(k, False)
            agg_rows.append(agg_row)

            # Per-entry rows
            pnl_frac  = (exit_prices_all - entry_prices) / entry_prices
            pnl_sigma = np.where(sigmas > 0, pnl_frac / sigmas, np.nan)
            for i in range(n):
                per_entry_rows.append({
                    "sigma_unit":    sigma_unit,
                    "policy":        cfg["policy"],
                    "config_idx":    ci,
                    "ticker":        tickers[i],
                    "date":          dates[i],
                    "session_bucket": session_bucket[i],
                    "is_runner_rc":  bool(is_runner[i]),
                    "sigma_val":     float(sigmas[i]),
                    "entry_price":   float(entry_prices[i]),
                    "exit_bar":      int(exit_bar_all[i]),
                    "exit_price":    float(exit_prices_all[i]),
                    "exit_reason":   exit_reason_all[i],
                    "pnl_frac":      float(pnl_frac[i]),
                    "pnl_sigma":     float(pnl_sigma[i]),
                    "mfe_30m_rc":    float(mfe_30m[i]),
                    "capture_30m":   float(pnl_frac[i] / mfe_30m[i]) if mfe_30m[i] > 0 else float("nan"),
                    # Policy params (for filtering without rejoining configs)
                    "k1":       float(cfg.get("k1",       float("nan"))),
                    "t_dead":   float(cfg.get("t_dead",   float("nan"))),
                    "a":        float(cfg.get("a",        float("nan"))),
                    "arm_mult": float(cfg.get("arm_mult", float("nan"))),
                    "g":        float(cfg.get("g",        float("nan"))),
                    "k_tp":     float(cfg.get("k_tp",     float("nan"))),
                    "k_sl":     float(cfg.get("k_sl",     float("nan"))),
                })

    per_df = pd.DataFrame(per_entry_rows)
    agg_df = pd.DataFrame(agg_rows)
    return per_df, agg_df


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    for p, name in [(PATHS_V1_PARQUET, "paths.parquet"), (SIGMA_PARQUET, "sigma_context.parquet")]:
        if not p.exists():
            log.error("%s not found at %s -- run prior tasks first", name, p)
            sys.exit(1)

    log.info("Loading %s", PATHS_V1_PARQUET)
    df_paths = pd.read_parquet(str(PATHS_V1_PARQUET))
    log.info("  %d entries", len(df_paths))

    log.info("Loading %s", SIGMA_PARQUET)
    df_sigma = pd.read_parquet(str(SIGMA_PARQUET))
    log.info("  %d sigma context rows", len(df_sigma))

    per_df, agg_df = run_sweep_v2(df_paths, df_sigma)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    per_df.to_parquet(str(SWEEP_PARQUET), index=False)
    log.info("Wrote %s (%d rows)", SWEEP_PARQUET, len(per_df))

    agg_df.to_parquet(str(SWEEP_AGG), index=False)
    log.info("Wrote %s (%d rows)", SWEEP_AGG, len(agg_df))

    # Summary: top 5 by primary sigma, confirm split
    n_entries    = len(df_sigma)
    sorted_dates = sorted(df_sigma["date"].unique())
    n_tune       = int(0.70 * len(sorted_dates))
    conf_dates   = set(sorted_dates[n_tune:])

    per_conf = per_df[per_df["date"].isin(conf_dates) & (per_df["sigma_unit"] == "primary")]
    agg_prim = agg_df[agg_df["sigma_unit"] == "primary"]
    top = agg_prim.nlargest(5, "median_capture_30m")[
        ["policy", "k1", "t_dead", "arm_mult", "g", "k_tp", "k_sl",
         "median_capture_30m", "pf", "cvar5_sigma", "n"]
    ]

    print("")
    print("[SEB-X v2 Task 3] Sweep complete: %d configs x 2 sigma units" % (len(agg_df) // 2))
    print("Top 5 by full-set median capture (sigma_unit=primary):")
    print(top.to_string(index=False))
    print("Sweep -> %s" % SWEEP_PARQUET)
    print("Agg   -> %s" % SWEEP_AGG)


if __name__ == "__main__":
    main()
