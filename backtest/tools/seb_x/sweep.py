"""
Phase SEB-X Task 3 -- Exit rule sweep (sigma-unit denominated).

Sweeps a stack of exit rules (B0, R1, R2, R3, sigma-TP/SL) against the
frozen forward paths cached in paths.parquet.

ALL rule thresholds are in sigma-units. Any raw-percent threshold is a bug.
Capture is measured against MFE(30m) from bar highs, not EOD.

Exit priority (when multiple rules fire at the same bar):
  R1 > R3 > R2 > B0 > horizon

Output: results/seb_x/sweep.parquet (one row per policy config × entry)
        results/seb_x/sweep_agg.parquet (one row per policy config, aggregated)
"""
from __future__ import annotations

import itertools
import logging
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT    = Path(__file__).resolve().parents[2]
OUTPUT_DIR    = _REPO_ROOT / "results" / "seb_x"
PATHS_PARQUET = OUTPUT_DIR / "paths.parquet"
SWEEP_PARQUET = OUTPUT_DIR / "sweep.parquet"
SWEEP_AGG     = OUTPUT_DIR / "sweep_agg.parquet"

# --------------------------------------------------------------------------
# Sweep grids (coarse -- chase plateaus not peaks)
# --------------------------------------------------------------------------
# B0: no params (baseline; VWAP cross)
# R1: hard floor stop
K1_GRID       = [1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0]
# R2: dead-money stop (T_dead in bars=minutes from entry, a = min favorable move in sigma)
T_DEAD_GRID   = [5, 10, 15, 20, 30]          # bars (= minutes)
A_GRID        = [-0.25, 0.0, 0.25, 0.5]      # sigma threshold to call trade "favorable"
# R3: vol trail (arm_mult = sigma level to arm trail, g = sigma give-back)
ARM_MULT_GRID = [1.0, 2.0, 3.0]
G_GRID        = [0.5, 1.0, 1.5, 2.0]
# sigma-TP/SL baseline (both in sigma units)
K_TP_GRID     = [2.0, 3.0, 4.0, 5.0]
K_SL_GRID     = [1.0, 1.5, 2.0]

HORIZON = -1   # sentinel: held to end of path


def _simulate_one(
    ph: np.ndarray,      # path_high
    pl: np.ndarray,      # path_low
    pc: np.ndarray,      # path_close
    pv: np.ndarray,      # path_vwap
    entry_price: float,
    sigma: float,
    # Rule flags
    use_b0:  bool = False,
    use_r1:  bool = False,  k1:       float = 2.0,
    use_r2:  bool = False,  t_dead:   int   = 15,  a: float = 0.0,
    use_r3:  bool = False,  arm_mult: float = 2.0, g: float = 1.0,
    use_tpsl: bool = False, k_tp:     float = 3.0, k_sl: float = 1.5,
) -> tuple[int, float, str]:
    """Simulate exit rules on a single path.

    Returns (exit_bar_0idx, exit_price, exit_reason).
    exit_bar = len(pc)-1 with reason='horizon' if no rule fires.
    """
    n = len(pc)
    if n == 0:
        return 0, entry_price, "horizon"

    # Precompute vectorised signals (avoids per-bar Python overhead)
    running_peak = np.maximum.accumulate(ph)  # cummax of highs

    # R1: first bar where low <= floor
    if use_r1 and sigma > 0:
        floor    = entry_price - k1 * sigma
        r1_mask  = pl <= floor
        r1_bar   = int(np.argmax(r1_mask)) if r1_mask.any() else n
    else:
        r1_bar = n

    # B0: first bar where close < vwap
    if use_b0:
        b0_mask = pc < pv
        b0_bar  = int(np.argmax(b0_mask)) if b0_mask.any() else n
    else:
        b0_bar = n

    # R3: armed trail -- once cummax >= entry+arm*sigma, exit if low <= peak-g*sigma
    if use_r3 and sigma > 0:
        arm_level = entry_price + arm_mult * sigma
        armed     = running_peak >= arm_level
        trail     = running_peak - g * sigma
        r3_mask   = armed & (pl <= trail)
        r3_bar    = int(np.argmax(r3_mask)) if r3_mask.any() else n
    else:
        r3_bar = n

    # R2: end-of-bar check at t_dead (0-indexed: t_dead - 1)
    if use_r2 and sigma > 0:
        td_idx = t_dead - 1
        if 0 <= td_idx < n:
            below_thresh = pc[td_idx] < entry_price + a * sigma
            below_vwap   = pc[td_idx] < pv[td_idx]
            r2_bar = td_idx if (below_thresh and below_vwap) else n
        else:
            r2_bar = n
    else:
        r2_bar = n

    # sigma-TP/SL: tp fires intrabar on high; sl fires intrabar on low
    if use_tpsl and sigma > 0:
        tp_level = entry_price + k_tp * sigma
        sl_level = entry_price - k_sl * sigma
        tp_mask  = ph >= tp_level
        sl_mask  = pl <= sl_level
        tp_bar   = int(np.argmax(tp_mask)) if tp_mask.any() else n
        sl_bar   = int(np.argmax(sl_mask)) if sl_mask.any() else n
    else:
        tp_bar = sl_bar = n

    # Priority: R1 > R3 > R2 > B0 > TP > SL > horizon
    # Ties: earlier bar wins; same bar -> priority order above
    candidates = [
        (r1_bar,  "R1"),
        (r3_bar,  "R3"),
        (r2_bar,  "R2"),
        (b0_bar,  "B0"),
        (tp_bar,  "TP"),
        (sl_bar,  "SL"),
    ]
    exit_bar   = n
    exit_reason = "horizon"
    for bar_idx, reason in candidates:
        if bar_idx < exit_bar:
            exit_bar    = bar_idx
            exit_reason = reason

    if exit_bar >= n:
        exit_bar    = n - 1
        exit_reason = "horizon"
        exit_price  = float(pc[-1])
    else:
        if exit_reason == "R1":
            exit_price = entry_price - k1 * sigma
        elif exit_reason == "R3":
            exit_price = float(running_peak[exit_bar]) - g * sigma
        elif exit_reason == "TP":
            exit_price = entry_price + k_tp * sigma
        elif exit_reason == "SL":
            exit_price = entry_price - k_sl * sigma
        else:  # R2 or B0: close fill
            exit_price = float(pc[exit_bar])

    return exit_bar, exit_price, exit_reason


def _metrics_for_exits(
    exit_prices: np.ndarray,
    entry_prices: np.ndarray,
    sigmas: np.ndarray,
    mfe_30m: np.ndarray,
    mfe_45m: np.ndarray,
) -> dict:
    """Aggregate metrics over all exits for a given policy config."""
    pnl_frac  = (exit_prices - entry_prices) / entry_prices
    pnl_sigma = np.where(sigmas > 0, pnl_frac / sigmas, np.nan)

    # Capture vs MFE(30m) -- only where MFE > 0
    cap_mask  = mfe_30m > 0
    capture   = np.where(cap_mask, pnl_frac / mfe_30m, np.nan)

    # P&L splits
    wins  = pnl_frac[pnl_frac > 0]
    loss  = pnl_frac[pnl_frac <= 0]

    gross_win  = float(wins.sum())  if len(wins)  > 0 else 0.0
    gross_loss = float(loss.sum())  if len(loss)  > 0 else 0.0
    pf         = gross_win / abs(gross_loss) if gross_loss < 0 else float("inf")

    # CVaR5: mean of the worst 5% of sigma-pnl
    pnl_s_valid = pnl_sigma[~np.isnan(pnl_sigma)]
    if len(pnl_s_valid) >= 20:
        cutoff = np.percentile(pnl_s_valid, 5)
        cvar5  = float(pnl_s_valid[pnl_s_valid <= cutoff].mean())
    else:
        cvar5 = float("nan")

    return {
        "n":                   int(len(pnl_frac)),
        "n_win":               int((pnl_frac > 0).sum()),
        "pf":                  float(pf),
        "cvar5_sigma":         cvar5,
        "median_pnl_frac":     float(np.median(pnl_frac)),
        "mean_pnl_frac":       float(np.mean(pnl_frac)),
        "median_pnl_sigma":    float(np.nanmedian(pnl_sigma)),
        "mean_pnl_sigma":      float(np.nanmean(pnl_sigma)),
        "median_capture_30m":  float(np.nanmedian(capture)),
        "mean_capture_30m":    float(np.nanmean(capture)),
        "p10_pnl_frac":        float(np.percentile(pnl_frac, 10)),
        "p25_pnl_frac":        float(np.percentile(pnl_frac, 25)),
        "p75_pnl_frac":        float(np.percentile(pnl_frac, 75)),
        "p90_pnl_frac":        float(np.percentile(pnl_frac, 90)),
    }


def build_policy_configs() -> list[dict]:
    """Return list of policy configuration dicts to sweep."""
    configs: list[dict] = []

    # B0 baseline (no params)
    configs.append({"policy": "B0", "use_b0": True})

    # sigma-TP/SL baseline
    for k_tp, k_sl in itertools.product(K_TP_GRID, K_SL_GRID):
        configs.append({
            "policy":   "TPSL",
            "use_tpsl": True,
            "k_tp":     k_tp,
            "k_sl":     k_sl,
        })

    # B0 + R1
    for k1 in K1_GRID:
        configs.append({
            "policy": "B0+R1",
            "use_b0": True,
            "use_r1": True,
            "k1":     k1,
        })

    # B0 + R1 + R2
    for k1, t_dead, a in itertools.product(K1_GRID, T_DEAD_GRID, A_GRID):
        configs.append({
            "policy": "B0+R1+R2",
            "use_b0": True,
            "use_r1": True,  "k1":     k1,
            "use_r2": True,  "t_dead": t_dead, "a": a,
        })

    # B0 + R3
    for arm_mult, g in itertools.product(ARM_MULT_GRID, G_GRID):
        configs.append({
            "policy":    "B0+R3",
            "use_b0":    True,
            "use_r3":    True,
            "arm_mult":  arm_mult,
            "g":         g,
        })

    # B0 + R1 + R3 (skip R2 to keep grid size manageable)
    for k1, arm_mult, g in itertools.product(K1_GRID[:4], ARM_MULT_GRID, G_GRID):
        configs.append({
            "policy":   "B0+R1+R3",
            "use_b0":   True,
            "use_r1":   True,  "k1":      k1,
            "use_r3":   True,  "arm_mult": arm_mult, "g": g,
        })

    # B0 + R1 + R2 + R3 (coarsest grid)
    for k1, t_dead, arm_mult, g in itertools.product(
        [1.5, 2.5],
        [10, 20],
        ARM_MULT_GRID,
        [0.5, 1.5],
    ):
        configs.append({
            "policy":   "B0+R1+R2+R3",
            "use_b0":   True,
            "use_r1":   True,  "k1":      k1,
            "use_r2":   True,  "t_dead":  t_dead, "a": 0.0,
            "use_r3":   True,  "arm_mult": arm_mult, "g": g,
        })

    log.info("Built %d policy configs", len(configs))
    return configs


def run_sweep(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Run all policy configs against all entries. Returns (per_entry_df, agg_df)."""

    # Preload arrays from list columns
    n = len(df)
    all_ph = [np.array(r, dtype=np.float64) for r in df["path_high"]]
    all_pl = [np.array(r, dtype=np.float64) for r in df["path_low"]]
    all_pc = [np.array(r, dtype=np.float64) for r in df["path_close"]]
    all_pv = [np.array(r, dtype=np.float64) for r in df["path_vwap"]]
    entry_prices = df["entry_price"].values.astype(np.float64)
    sigmas       = df["sigma"].values.astype(np.float64)
    mfe_30m      = df["mfe_30m_rc"].values.astype(np.float64)
    mfe_45m      = df["mfe_45m_rc"].values.astype(np.float64)
    tickers      = df["ticker"].values
    dates        = df["date"].values
    is_runner    = df["is_runner_rc"].values.astype(bool)

    configs = build_policy_configs()
    n_configs = len(configs)
    log.info("Sweeping %d configs x %d entries", n_configs, n)

    per_entry_rows: list[dict] = []
    agg_rows:       list[dict] = []

    for ci, cfg in enumerate(configs):
        if ci % 50 == 0:
            log.info("  sweep config %d / %d  (%s)", ci, n_configs, cfg["policy"])

        exit_prices_all = np.empty(n, dtype=np.float64)
        exit_bar_all    = np.empty(n, dtype=np.int64)
        exit_reason_all = [""] * n

        for i in range(n):
            bar, price, reason = _simulate_one(
                all_ph[i], all_pl[i], all_pc[i], all_pv[i],
                entry_prices[i], sigmas[i],
                use_b0   = cfg.get("use_b0",   False),
                use_r1   = cfg.get("use_r1",   False),  k1       = cfg.get("k1",       2.0),
                use_r2   = cfg.get("use_r2",   False),  t_dead   = cfg.get("t_dead",   15),  a = cfg.get("a", 0.0),
                use_r3   = cfg.get("use_r3",   False),  arm_mult = cfg.get("arm_mult", 2.0), g = cfg.get("g", 1.0),
                use_tpsl = cfg.get("use_tpsl", False),  k_tp     = cfg.get("k_tp",     3.0), k_sl = cfg.get("k_sl", 1.5),
            )
            exit_prices_all[i] = price
            exit_bar_all[i]    = bar
            exit_reason_all[i] = reason

        metrics = _metrics_for_exits(
            exit_prices_all, entry_prices, sigmas, mfe_30m, mfe_45m
        )

        # -- Aggregate row --
        agg_row = {**cfg, **metrics}
        # Clean NaN param slots (unset params)
        for k in ("k1", "t_dead", "a", "arm_mult", "g", "k_tp", "k_sl"):
            agg_row.setdefault(k, float("nan"))
        for k in ("use_b0", "use_r1", "use_r2", "use_r3", "use_tpsl"):
            agg_row.setdefault(k, False)
        agg_rows.append(agg_row)

        # -- Per-entry rows --
        pnl_frac  = (exit_prices_all - entry_prices) / entry_prices
        pnl_sigma = np.where(sigmas > 0, pnl_frac / sigmas, np.nan)
        for i in range(n):
            per_entry_rows.append({
                "policy":       cfg["policy"],
                "config_idx":   ci,
                "ticker":       tickers[i],
                "date":         dates[i],
                "is_runner_rc": bool(is_runner[i]),
                "sigma":        float(sigmas[i]),
                "entry_price":  float(entry_prices[i]),
                "exit_bar":     int(exit_bar_all[i]),
                "exit_price":   float(exit_prices_all[i]),
                "exit_reason":  exit_reason_all[i],
                "pnl_frac":     float(pnl_frac[i]),
                "pnl_sigma":    float(pnl_sigma[i]),
                "mfe_30m_rc":   float(mfe_30m[i]),
                "capture_30m":  float(pnl_frac[i] / mfe_30m[i]) if mfe_30m[i] > 0 else float("nan"),
                # Params (for easy filtering)
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

    if not PATHS_PARQUET.exists():
        log.error("paths.parquet not found -- run build_paths.py first")
        sys.exit(1)

    log.info("Loading %s", PATHS_PARQUET)
    df = pd.read_parquet(str(PATHS_PARQUET))
    log.info("  %d entries", len(df))

    per_df, agg_df = run_sweep(df)

    per_df.to_parquet(str(SWEEP_PARQUET), index=False)
    log.info("Wrote %s (%d rows)", SWEEP_PARQUET, len(per_df))

    agg_df.to_parquet(str(SWEEP_AGG), index=False)
    log.info("Wrote %s (%d configs)", SWEEP_AGG, len(agg_df))

    # Quick summary: top 5 configs by median capture
    top = agg_df.nlargest(5, "median_capture_30m")[
        ["policy", "k1", "t_dead", "arm_mult", "g", "k_tp", "k_sl",
         "median_capture_30m", "median_pnl_sigma", "pf", "cvar5_sigma", "n"]
    ]
    print("")
    print("[SEB-X Task 3] Sweep complete: %d configs" % len(agg_df))
    print("Top 5 by median capture(30m):")
    print(top.to_string(index=False))
    print("Sweep -> %s" % SWEEP_PARQUET)
    print("Agg   -> %s" % SWEEP_AGG)

    return agg_df


if __name__ == "__main__":
    main()
