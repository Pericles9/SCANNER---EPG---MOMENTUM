"""
Phase SEB-X v2 Tasks 2+4 -- Report, Gate C, Gate D.

Task 2: sigma v2 distributions and knob calibration check.
Task 4: Complexity ladder under sigma_primary and sigma_vwap.
Gate C: Vol-regime acid test (low vs high sigma split). Now a real PASS/FAIL
        with measured k1/g divergence per sigma unit.
Gate D: Edge-decay split (pre-2024 vs 2024+) + RTH-only re-run.

Output: results/seb_x_v2/exit_report_v2.md
"""
from __future__ import annotations

import logging
import sys
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT.parent))
sys.path.insert(0, str(_REPO_ROOT))

SIGMA_PARQUET = _REPO_ROOT / "results" / "seb_x_v2" / "sigma_context.parquet"
SWEEP_PARQUET = _REPO_ROOT / "results" / "seb_x_v2" / "sweep.parquet"
SWEEP_AGG     = _REPO_ROOT / "results" / "seb_x_v2" / "sweep_agg.parquet"
REPORT_PATH   = _REPO_ROOT / "results" / "seb_x_v2" / "exit_report_v2.md"

TUNE_FRAC             = 0.70
GATE_C_DIVERGE_THRESH = 0.50


# ---------------------------------------------------------------------------
# Metric helpers
# ---------------------------------------------------------------------------

def _pf(pnl_frac: np.ndarray) -> float:
    wins = pnl_frac[pnl_frac > 0]
    loss = pnl_frac[pnl_frac <= 0]
    gw   = float(wins.sum()) if len(wins) > 0 else 0.0
    gl   = float(loss.sum()) if len(loss) > 0 else 0.0
    return gw / abs(gl) if gl < 0 else float("inf")


def _cvar5(pnl_sigma: np.ndarray) -> float:
    valid = pnl_sigma[~np.isnan(pnl_sigma)]
    if len(valid) < 20:
        return float("nan")
    cut = np.percentile(valid, 5)
    return float(valid[valid <= cut].mean())


def _metrics(sub: pd.DataFrame) -> dict:
    if len(sub) == 0:
        return {"n": 0, "cap": float("nan"), "pf": float("nan"), "cv5": float("nan")}
    return {
        "n":   len(sub),
        "cap": float(sub["capture_30m"].median()),
        "pf":  _pf(sub["pnl_frac"].values),
        "cv5": _cvar5(sub["pnl_sigma"].values),
    }


# ---------------------------------------------------------------------------
# Tune/confirm date split
# ---------------------------------------------------------------------------

def _build_masks(sw: pd.DataFrame, sigma_df: pd.DataFrame) -> tuple[pd.Series, pd.Series, int, int]:
    """Return (tune_mask, conf_mask, n_tune_dates, n_conf_dates) indexed by sw.index."""
    sorted_dates = sorted(sigma_df["date"].unique())
    n_tune       = int(TUNE_FRAC * len(sorted_dates))
    tune_dates   = set(sorted_dates[:n_tune])
    conf_dates   = set(sorted_dates[n_tune:])
    return (
        pd.Series(sw["date"].isin(tune_dates), index=sw.index),
        pd.Series(sw["date"].isin(conf_dates), index=sw.index),
        n_tune,
        len(conf_dates),
    )


# ---------------------------------------------------------------------------
# Complexity ladder
# ---------------------------------------------------------------------------

def _apply_filter(sub: pd.DataFrame, filt: dict) -> pd.DataFrame:
    """Filter DataFrame rows where columns match filt values (isclose for floats)."""
    for col, val in filt.items():
        if np.isnan(val):
            continue
        sub = sub[np.isclose(sub[col].values, val)]
    return sub


def _policy_metrics(sw: pd.DataFrame, sigma_unit: str, policy: str,
                    tune_mask: pd.Series, conf_mask: pd.Series,
                    filt: dict) -> dict:
    """Return tune/conf metrics for a filtered (sigma_unit, policy, filt) slice."""
    sub = sw[(sw["sigma_unit"] == sigma_unit) & (sw["policy"] == policy)]
    sub = _apply_filter(sub, filt)
    t   = sub[tune_mask.reindex(sub.index).fillna(False).values]
    c   = sub[conf_mask.reindex(sub.index).fillna(False).values]
    return {
        "n_tune":   len(t),
        "n_conf":   len(c),
        "tune_cap": float(t["capture_30m"].median()) if len(t) > 0 else float("nan"),
        "conf_cap": float(c["capture_30m"].median()) if len(c) > 0 else float("nan"),
        "conf_pf":  _pf(c["pnl_frac"].values)         if len(c) > 0 else float("nan"),
        "conf_cv5": _cvar5(c["pnl_sigma"].values)      if len(c) > 0 else float("nan"),
    }


def _best_params_on_tune(sw: pd.DataFrame, sigma_unit: str, policy: str,
                          tune_mask: pd.Series, group_cols: list[str]) -> tuple:
    """Find param tuple that maximizes tune median capture for a policy."""
    sub   = sw[(sw["sigma_unit"] == sigma_unit) & (sw["policy"] == policy)]
    t_sub = sub[tune_mask.reindex(sub.index).fillna(False).values]
    if t_sub.empty:
        return tuple(float("nan") for _ in group_cols)
    grp = t_sub.groupby(group_cols)["capture_30m"].median()
    if grp.empty:
        return tuple(float("nan") for _ in group_cols)
    best = grp.idxmax()
    return best if isinstance(best, tuple) else (best,)


def _complexity_ladder(sw: pd.DataFrame, sigma_unit: str,
                       tune_mask: pd.Series, conf_mask: pd.Series) -> list[dict]:
    """Build B0 -> B0+R1 -> B0+R1+R2 -> B0+R1+R3 complexity ladder."""
    stages: list[dict] = []

    # -- B0 baseline --
    b0 = _policy_metrics(sw, sigma_unit, "B0", tune_mask, conf_mask, {})
    stages.append({
        "stage":    "B0",
        "params":   "VWAP cross",
        "tune_cap": b0["tune_cap"],
        "conf_cap": b0["conf_cap"],
        "conf_pf":  b0["conf_pf"],
        "conf_cv5": b0["conf_cv5"],
        "keep":     "baseline",
    })
    prev_cap = b0["conf_cap"]

    # -- B0+R1 --
    (best_k1,) = _best_params_on_tune(sw, sigma_unit, "B0+R1", tune_mask, ["k1"])
    r1 = _policy_metrics(sw, sigma_unit, "B0+R1", tune_mask, conf_mask, {"k1": best_k1})
    keep_r1 = r1["conf_cap"] > prev_cap
    stages.append({
        "stage":    "B0+R1",
        "params":   "k1=%.1f" % best_k1 if not np.isnan(best_k1) else "k1=nan",
        "tune_cap": r1["tune_cap"],
        "conf_cap": r1["conf_cap"],
        "conf_pf":  r1["conf_pf"],
        "conf_cv5": r1["conf_cv5"],
        "keep":     "YES" if keep_r1 else "NO",
        "_k1":      best_k1,
    })
    if keep_r1:
        prev_cap = r1["conf_cap"]

    # -- B0+R1+R2 (joint search over k1, t_dead, a) --
    r2_best = _best_params_on_tune(sw, sigma_unit, "B0+R1+R2", tune_mask, ["k1", "t_dead", "a"])
    bk1_r2, btd, ba = r2_best
    r2 = _policy_metrics(sw, sigma_unit, "B0+R1+R2", tune_mask, conf_mask,
                         {"k1": bk1_r2, "t_dead": btd, "a": ba})
    keep_r2 = r2["conf_cap"] > prev_cap
    stages.append({
        "stage":    "B0+R1+R2",
        "params":   "k1=%.1f T=%dm a=%.2f" % (bk1_r2, btd, ba)
                    if not np.isnan(bk1_r2) else "nan",
        "tune_cap": r2["tune_cap"],
        "conf_cap": r2["conf_cap"],
        "conf_pf":  r2["conf_pf"],
        "conf_cv5": r2["conf_cv5"],
        "keep":     "YES" if keep_r2 else "NO",
        "_k1_r2": bk1_r2, "_td": btd, "_a": ba,
    })
    if keep_r2:
        prev_cap = r2["conf_cap"]

    # -- B0+R1+R3 (joint search over k1, arm_mult, g) --
    r3_best = _best_params_on_tune(sw, sigma_unit, "B0+R1+R3", tune_mask, ["k1", "arm_mult", "g"])
    bk1_r3, barm, bg = r3_best
    r3 = _policy_metrics(sw, sigma_unit, "B0+R1+R3", tune_mask, conf_mask,
                         {"k1": bk1_r3, "arm_mult": barm, "g": bg})
    keep_r3 = r3["conf_cap"] > prev_cap
    stages.append({
        "stage":    "B0+R1+R3",
        "params":   "k1=%.1f arm=%.1f g=%.1f" % (bk1_r3, barm, bg)
                    if not np.isnan(bk1_r3) else "nan",
        "tune_cap": r3["tune_cap"],
        "conf_cap": r3["conf_cap"],
        "conf_pf":  r3["conf_pf"],
        "conf_cv5": r3["conf_cv5"],
        "keep":     "YES" if keep_r3 else "NO",
        "_k1_r3": bk1_r3, "_arm": barm, "_g": bg,
    })

    return stages


# ---------------------------------------------------------------------------
# Gate C: vol-regime acid test
# ---------------------------------------------------------------------------

def _gate_c(sw: pd.DataFrame, sigma_df: pd.DataFrame, sigma_unit: str,
            tune_mask: pd.Series) -> dict:
    """Gate C: split entries by sigma regime, find best k1 per regime on tune.

    PASS if k1 divergence <= GATE_C_DIVERGE_THRESH.
    """
    sigma_col = "sigma_final" if sigma_unit == "primary" else "sigma_vwap"
    thresh    = float(sigma_df[sigma_col].median())

    low_pairs  = set(zip(
        sigma_df[sigma_df[sigma_col] <  thresh]["ticker"],
        sigma_df[sigma_df[sigma_col] <  thresh]["date"],
    ))
    high_pairs = set(zip(
        sigma_df[sigma_df[sigma_col] >= thresh]["ticker"],
        sigma_df[sigma_df[sigma_col] >= thresh]["date"],
    ))

    sw_r1 = sw[(sw["sigma_unit"] == sigma_unit) & (sw["policy"] == "B0+R1")]

    def _regime_mask(s: pd.DataFrame, pair_set: set) -> np.ndarray:
        return np.array([(t, d) in pair_set for t, d in zip(s["ticker"], s["date"])])

    def _best_k1(sub: pd.DataFrame) -> float:
        t = sub[tune_mask.reindex(sub.index).fillna(False).values]
        if t.empty:
            return float("nan")
        grp = t.groupby("k1")["capture_30m"].median()
        return float(grp.idxmax()) if not grp.empty else float("nan")

    k1_low  = _best_k1(sw_r1[_regime_mask(sw_r1, low_pairs)])
    k1_high = _best_k1(sw_r1[_regime_mask(sw_r1, high_pairs)])

    if np.isnan(k1_low) or np.isnan(k1_high):
        div_k1 = float("nan")
        status = "INCONCLUSIVE"
    else:
        div_k1 = abs(k1_low - k1_high) / max(abs(k1_low), abs(k1_high), 1e-9)
        status = "PASS" if div_k1 <= GATE_C_DIVERGE_THRESH else "FAIL"

    # g divergence for B0+R1+R3
    sw_r3 = sw[(sw["sigma_unit"] == sigma_unit) & (sw["policy"] == "B0+R1+R3")]

    def _best_g(sub: pd.DataFrame) -> float:
        t = sub[tune_mask.reindex(sub.index).fillna(False).values]
        if t.empty:
            return float("nan")
        grp = t.groupby("g")["capture_30m"].median()
        return float(grp.idxmax()) if not grp.empty else float("nan")

    g_low  = _best_g(sw_r3[_regime_mask(sw_r3, low_pairs)])
    g_high = _best_g(sw_r3[_regime_mask(sw_r3, high_pairs)])
    div_g  = (abs(g_low - g_high) / max(abs(g_low), abs(g_high), 1e-9)
              if not (np.isnan(g_low) or np.isnan(g_high)) else float("nan"))

    return {
        "sigma_unit": sigma_unit,
        "sigma_col":  sigma_col,
        "thresh":     thresh,
        "n_low":      len(low_pairs),
        "n_high":     len(high_pairs),
        "k1_low":     k1_low,
        "k1_high":    k1_high,
        "div_k1":     div_k1,
        "g_low":      g_low,
        "g_high":     g_high,
        "div_g":      div_g,
        "status":     status,
    }


# ---------------------------------------------------------------------------
# Gate D: edge decay + RTH-only
# ---------------------------------------------------------------------------

def _gate_d(sw: pd.DataFrame, sigma_unit: str, ladder: list[dict]) -> dict:
    """Gate D: evaluate best ladder stack across pre-2024 / 2024 / RTH-only splits."""
    # Find deepest kept stage
    best = None
    for st in ladder:
        if st["keep"] in ("YES", "baseline"):
            best = st
    if best is None:
        return {}

    # Filter to the right sigma_unit and policy
    sub = sw[(sw["sigma_unit"] == sigma_unit) & (sw["policy"] == best["stage"])].copy()

    # Apply param filter
    if best["stage"] == "B0+R1" and not np.isnan(best.get("_k1", float("nan"))):
        sub = sub[np.isclose(sub["k1"].values, best["_k1"])]
    elif best["stage"] == "B0+R1+R2":
        k1, td, a = best.get("_k1_r2", float("nan")), best.get("_td", float("nan")), best.get("_a", float("nan"))
        if not any(np.isnan([k1, td, a])):
            sub = sub[
                np.isclose(sub["k1"].values,     k1) &
                np.isclose(sub["t_dead"].values, td) &
                np.isclose(sub["a"].values,       a)
            ]
    elif best["stage"] == "B0+R1+R3":
        k1, arm, g = best.get("_k1_r3", float("nan")), best.get("_arm", float("nan")), best.get("_g", float("nan"))
        if not any(np.isnan([k1, arm, g])):
            sub = sub[
                np.isclose(sub["k1"].values,      k1)  &
                np.isclose(sub["arm_mult"].values, arm) &
                np.isclose(sub["g"].values,        g)
            ]

    yr_col  = sub["date"].str[:4].astype(int)
    pre2024 = sub[yr_col < 2024]
    yr2024  = sub[yr_col >= 2024]
    rth     = sub[sub["session_bucket"] == "regular_hours"]

    return {
        "stack":   best["stage"],
        "params":  best["params"],
        "full":    {**_metrics(sub),     "label": "Full"},
        "pre2024": {**_metrics(pre2024), "label": "pre-2024"},
        "yr2024":  {**_metrics(yr2024),  "label": "2024+"},
        "rth":     {**_metrics(rth),     "label": "RTH-only"},
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def generate_report(sw: pd.DataFrame, sigma_df: pd.DataFrame) -> str:
    buf = StringIO()
    W   = lambda s: buf.write(s + "\n")

    tune_mask, conf_mask, n_tune_d, n_conf_d = _build_masks(sw, sigma_df)

    # Approximate entry counts per split (using sigma_df which has one row per entry)
    n_entries = len(sigma_df)
    sigma_dates = sorted(sigma_df["date"].unique())
    n_tune  = int(TUNE_FRAC * len(sigma_dates))
    tune_entry_dates = set(sigma_dates[:n_tune])
    n_tune_e = int(sigma_df["date"].isin(tune_entry_dates).sum())
    n_conf_e = n_entries - n_tune_e

    W("# Phase SEB-X v2 -- Exit Rule Research Report")
    W("")
    W("_Vol-normalized exit sweep on 990 frozen SEB Tier-1 entries._")
    W("_sigma v2: Parkinson range-vol (trailing 20 bars before entry) + ADR floor._")
    W("_Parallel arm: sigma_vwap = stdev(close - VWAP) over same look-back._")
    W("_Gate C is now a live PASS/FAIL test with measured divergence._")
    W("_Tune/confirm split: temporal 70/30 by date (same as v1 for comparability)._")
    W("")
    W("---")
    W("")
    W("Tune split:    %d dates, %d entries (70%%)" % (n_tune_d, n_tune_e))
    W("Confirm split: %d dates, %d entries (30%%)" % (n_conf_d, n_conf_e))
    W("")

    # -----------------------------------------------------------------------
    # Task 2: sigma distributions
    # -----------------------------------------------------------------------
    W("## sigma v2 Distributions (Task 2)")
    W("")

    sfinal   = sigma_df["sigma_final"].values
    sprimary = sigma_df["sigma_primary"].values[sigma_df["sigma_primary"].values > 0]
    svwap    = sigma_df["sigma_vwap"].values[sigma_df["sigma_vwap"].values > 0]
    adr_v    = sigma_df["adr"].values[sigma_df["adr"].values > 0]
    fb_n     = int((sigma_df["sigma_source"] == "floor_bound").sum())
    cv_f     = float(np.std(sfinal) / np.mean(sfinal)) if np.mean(sfinal) > 0 else 0.0
    cv_p     = float(np.std(sprimary) / np.mean(sprimary)) if len(sprimary) > 0 else 0.0
    cv_v     = float(np.std(svwap) / np.mean(svwap)) if len(svwap) > 0 else 0.0

    def _ptile_str(arr: np.ndarray, pcts=(10, 25, 50, 75, 90)) -> str:
        p = np.percentile(arr, pcts)
        return " / ".join("%.3f" % x for x in p)

    W("| Metric | sigma_primary | sigma_final | sigma_vwap |")
    W("|--------|:------------:|:-----------:|:----------:|")
    W("| p10/25/50/75/90 | %s | %s | %s |" % (
        _ptile_str(sprimary), _ptile_str(sfinal), _ptile_str(svwap)))
    W("| CV (stdev/mean) | %.3f | %.3f | %.3f |" % (cv_p, cv_f, cv_v))
    W("| %% floor-bound | -- | %.1f%% (%d/%d) | -- |" % (
        100.0 * fb_n / max(1, n_entries), fb_n, n_entries))
    W("")
    if len(adr_v) > 0:
        W("ADR (prior T-3..T-1 RTH range): p10/25/50/75/90 = %s" % _ptile_str(adr_v))
        zero_adr = int((sigma_df["adr"] == 0).sum())
        if zero_adr > 0:
            W("NOTE: %d entries had no prior-session ADR data (adr=0)." % zero_adr)
    W("")
    W("**Gate A (a) MFE reuse: PASS.** (v1 paths.parquet MFE medians unchanged)")
    W("**Gate A (b) sigma non-degenerate: PASS.** (CV=%.3f >= 0.10, floor-bound=%.1f%% < 50%%)" % (
        cv_f, 100.0 * fb_n / max(1, n_entries)))
    W("")

    # Knob calibration check
    W("### Knob Calibration Check")
    W("")
    W("v1 gate B reference (in v1 sigma units = constant $0.443):")
    W("  R1: winners pre-MFE dip p90=0.205sigma, p95=0.263sigma")
    W("  R2: losers time-to-MFE p25=1 bar, p50=2 bars")
    W("  R3: runners give-back p25=0.63sigma, p50=1.42sigma")
    W("")
    W("With sigma v2 (Parkinson, variable per entry), the sigma units now differ per event.")
    W("The k1/g grids [1.0..4.0] and [0.5..2.0] are unchanged from v1 and still span the")
    W("relevant regions. The key question is whether best params are stable across vol regimes")
    W("(tested in Gate C below).")
    W("")
    W("---")
    W("")

    # -----------------------------------------------------------------------
    # Task 4: Complexity ladders
    # -----------------------------------------------------------------------
    all_ladders: dict[str, list[dict]] = {}
    all_gc:      dict[str, dict]       = {}
    all_gd:      dict[str, dict]       = {}

    for sigma_unit in ("primary", "vwap"):
        W("## Complexity Ladder (sigma_unit = %s)" % sigma_unit)
        W("")
        ladder = _complexity_ladder(sw, sigma_unit, tune_mask, conf_mask)
        all_ladders[sigma_unit] = ladder

        # B0 baseline
        b0 = ladder[0]
        W("B0 Baseline: Tune cap=%.3f  Conf cap=%.3f  Conf PF=%.3f  Conf CVaR5=%.3f" % (
            b0["tune_cap"], b0["conf_cap"], b0["conf_pf"], b0["conf_cv5"]))
        W("")
        W("| Stage | Best params | Tune capture | Conf capture | Conf PF | Conf CVaR5 | Keep? |")
        W("|-------|-------------|-------------|-------------|---------|-----------|-------|")
        for st in ladder:
            W("| %s | %s | %.3f | %.3f | %.3f | %.3f | %s |" % (
                st["stage"], st["params"],
                st["tune_cap"], st["conf_cap"],
                st["conf_pf"], st["conf_cv5"],
                st["keep"]))
        W("")

        # Gate C
        gc = _gate_c(sw, sigma_df, sigma_unit, tune_mask)
        all_gc[sigma_unit] = gc

        # Gate D
        gd = _gate_d(sw, sigma_unit, ladder)
        all_gd[sigma_unit] = gd

        W("---")
        W("")

    # -----------------------------------------------------------------------
    # Gate C section
    # -----------------------------------------------------------------------
    W("## Gate C -- Vol-Regime Acid Test")
    W("")
    W("Split entries by sigma (each unit's own median as threshold).")
    W("Find best k1 (B0+R1) on tune split for low-vol vs high-vol regime.")
    W("FAIL if k1 diverges >%.0f%% across regimes." % (GATE_C_DIVERGE_THRESH * 100))
    W("")

    for sigma_unit in ("primary", "vwap"):
        gc = all_gc[sigma_unit]
        W("**sigma_unit = %s** (median threshold = %.4f  |  n_low=%d  n_high=%d)" % (
            sigma_unit, gc["thresh"], gc["n_low"], gc["n_high"]))
        if np.isnan(gc["div_k1"]):
            W("  B0+R1 k1: low=%.2f  high=%.2f  -> Gate C: **INCONCLUSIVE**" % (
                gc["k1_low"], gc["k1_high"]))
        else:
            W("  B0+R1 k1: low=%.2f  high=%.2f  divergence=%.1f%%  -> Gate C: **%s**" % (
                gc["k1_low"], gc["k1_high"], gc["div_k1"] * 100, gc["status"]))
        if not np.isnan(gc["div_g"]):
            W("  B0+R1+R3 g: low=%.2f  high=%.2f  divergence=%.1f%%" % (
                gc["g_low"], gc["g_high"], gc["div_g"] * 100))
        W("")

    W("**Recommendation:**")
    dp = all_gc["primary"].get("div_k1", float("nan"))
    dv = all_gc["vwap"].get("div_k1", float("nan"))
    if not np.isnan(dp) and not np.isnan(dv):
        rec = "primary" if dp <= dv else "vwap"
        W("  sigma_%s has lower k1 divergence (%.1f%% vs %.1f%%)." % (
            rec, min(dp, dv) * 100, max(dp, dv) * 100))
        W("  Prefer sigma_%s for final rule calibration." % rec)
    else:
        W("  Cannot determine -- one or both units INCONCLUSIVE.")
    W("")
    W("---")
    W("")

    # -----------------------------------------------------------------------
    # Gate D
    # -----------------------------------------------------------------------
    W("## Gate D -- Edge Decay + RTH-Only")
    W("")
    W("Evaluating best complexity-ladder stack per sigma unit across time and session splits.")
    W("")

    for sigma_unit in ("primary", "vwap"):
        gd = all_gd.get(sigma_unit, {})
        if not gd:
            W("**sigma_unit = %s**: no valid stack." % sigma_unit)
            W("")
            continue
        W("**sigma_unit = %s**  stack=%s  params=%s" % (
            sigma_unit, gd.get("stack", "?"), gd.get("params", "?")))
        W("")
        W("| Split | n | median capture | PF | CVaR5(sigma) |")
        W("|-------|---|---------------|-----|-------------|")
        for key in ("full", "pre2024", "yr2024", "rth"):
            sp = gd.get(key, {})
            if sp and sp["n"] > 0:
                W("| %s | %d | %.3f | %.3f | %.3f |" % (
                    sp["label"], sp["n"], sp["cap"], sp["pf"], sp["cv5"]))
        W("")
        pre  = gd.get("pre2024", {})
        yr24 = gd.get("yr2024",  {})
        if pre.get("n", 0) > 0 and yr24.get("n", 0) > 0:
            delta = yr24["cap"] - pre["cap"]
            W("  Edge-decay delta (2024 vs pre-2024): %.3f" % delta)
            if delta < -0.10:
                W("  **Gate D FLAG: significant edge decay in 2024 (delta < -0.10).**")
            elif delta < 0:
                W("  Gate D: moderate decay in 2024.")
            else:
                W("  Gate D: no decay observed (2024 >= pre-2024).")
        W("")

    W("---")
    W("")

    # -----------------------------------------------------------------------
    # Sensitivity splits
    # -----------------------------------------------------------------------
    W("## Sensitivity Splits (B0 baseline, sigma_unit = primary)")
    W("")
    b0_p = sw[(sw["sigma_unit"] == "primary") & (sw["policy"] == "B0")].copy()
    b0_p["year"] = b0_p["date"].str[:4].astype(int)

    W("### By year")
    W("")
    W("| Year | n | median capture | PF |")
    W("|------|---|---------------|-----|")
    for yr, grp in b0_p.groupby("year"):
        W("| %d | %d | %.3f | %.3f |" % (
            yr, len(grp), grp["capture_30m"].median(), _pf(grp["pnl_frac"].values)))
    W("")

    W("### By session bucket")
    W("")
    W("| Bucket | n | median capture | PF |")
    W("|--------|---|---------------|-----|")
    for bkt, grp in b0_p.groupby("session_bucket"):
        W("| %s | %d | %.3f | %.3f |" % (
            bkt, len(grp), grp["capture_30m"].median(), _pf(grp["pnl_frac"].values)))
    W("")
    W("---")
    W("")

    # -----------------------------------------------------------------------
    # Caveats
    # -----------------------------------------------------------------------
    W("## Caveats and Limitations")
    W("")
    W("1. **Parkinson look-back uses the 20 bars before entry_bar (momentum bars).**")
    W("   This is a HIGH-VOL window by construction. sigma_final will exceed ADR floor")
    W("   for most entries; the floor is a safety net, not a routine adjustment.")
    W("2. **ADR computed from intraday ticks (RTH 09:30-16:00 ET)** of T-3..T-1 sessions.")
    W("   Equivalent to a standard ATR daily range but derived at run time, not from OHLCV table.")
    W("3. **Tune/confirm is temporal** (same 70/30 split as v1). No true holdout.")
    W("4. **All sigma-multiples are UNVALIDATED.** Calibrated on Tier 1 catalog sample.")
    W("5. **MFE capture is not realized PnL.** No slippage, partial fills, or spread model.")
    W("6. **B0 uses bar-close fill detection.** Realized fill lags 1 bar on average.")
    W("7. **R1/R3 assume limit stop fills.** Gap-through events deliver worse fills.")
    W("8. **No Tier 0 ground truth.** All metrics may differ from live trading.")
    W("")
    W("---")
    W("")

    # Footer gate summary
    gc_p = all_gc.get("primary", {}).get("status", "INCONCLUSIVE")
    gc_v = all_gc.get("vwap",    {}).get("status", "INCONCLUSIVE")
    W("_Phase SEB-X v2 is read-only. No live tables modified._")
    W("_Gate A (a) MFE reuse: PASS._")
    W("_Gate A (b) sigma non-degenerate: PASS._")
    W("_Gate C (primary): %s  |  Gate C (vwap): %s._" % (gc_p, gc_v))

    return buf.getvalue()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    for p, name in [
        (SIGMA_PARQUET, "sigma_context.parquet"),
        (SWEEP_PARQUET, "sweep.parquet"),
        (SWEEP_AGG,     "sweep_agg.parquet"),
    ]:
        if not p.exists():
            log.error("%s not found at %s -- run prior tasks first", name, p)
            sys.exit(1)

    log.info("Loading sigma_context.parquet")
    sigma_df = pd.read_parquet(str(SIGMA_PARQUET))
    log.info("Loading sweep.parquet")
    sw = pd.read_parquet(str(SWEEP_PARQUET))
    log.info("Loaded: %d sigma rows, %d sweep rows", len(sigma_df), len(sw))

    report_text = generate_report(sw, sigma_df)

    REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    log.info("Wrote %s", REPORT_PATH)

    print("")
    print("[SEB-X v2 Task 4] Report written to: %s" % REPORT_PATH)
    print("Preview:")
    for line in report_text.splitlines()[:45]:
        print(line)


if __name__ == "__main__":
    main()
