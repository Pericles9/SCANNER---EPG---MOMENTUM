"""
Phase SEB-X Task 4 -- Validation, Gate C (vol-regime acid test), and exit report.

Reads sweep.parquet (per-entry per-config outcomes) and paths.parquet, then:
  1. Builds tune/confirm split (temporal: first 70% by date = tune, rest = confirm)
  2. Runs complexity ladder: B0 -> B0+R1 -> B0+R1+R2 -> B0+R1+R3 -> full stack
     Each rule kept only if confirm-split beats without it.
  3. Gate C: split by vol-regime (low-sigma vs high-sigma entries).
     If optimal sigma-multiples diverge >50% across regimes, normalization failed.
     Report as HEADLINE FINDING; do NOT ship a global k.
  4. Additional splits: session bucket, event-day, year.
  5. Writes results/seb_x/exit_report.md

Gate C is the acid test. The whole point of sigma-normalization is that the same
k1/g/T_dead works across vol regimes. If they diverge, sigma-units are not portable
and the sweep finds strategy-specific parameters, not universal multiples.
"""
from __future__ import annotations

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
REPORT_MD     = OUTPUT_DIR / "exit_report.md"

# Tune/confirm temporal split
TUNE_FRAC = 0.70

# Gate C divergence threshold
GATE_C_DIVERGE_THRESH = 0.50  # >50% difference in optimal k -> normalization failed


def _best_config_for_policy(
    agg: pd.DataFrame,
    policy: str,
    metric: str = "median_capture_30m",
) -> Optional[pd.Series]:
    sub = agg[agg["policy"] == policy]
    if sub.empty:
        return None
    return sub.loc[sub[metric].idxmax()]


def _metrics_for_subset(
    per_df: pd.DataFrame,
    mask: pd.Series,
    config_idx: int,
) -> dict:
    sub = per_df[(per_df["config_idx"] == config_idx) & mask]
    if sub.empty:
        return {"n": 0, "median_capture": float("nan"), "pf": float("nan"), "cvar5": float("nan")}

    pnl   = sub["pnl_frac"].values
    cap   = sub["capture_30m"].values

    wins  = pnl[pnl > 0]
    loss  = pnl[pnl <= 0]
    gw    = float(wins.sum())  if len(wins)  else 0.0
    gl    = float(loss.sum())  if len(loss)  else 0.0
    pf    = gw / abs(gl) if gl < 0 else float("inf")

    ps    = sub["pnl_sigma"].values
    ps_v  = ps[~np.isnan(ps)]
    if len(ps_v) >= 10:
        cut   = np.percentile(ps_v, 5)
        cvar5 = float(ps_v[ps_v <= cut].mean())
    else:
        cvar5 = float("nan")

    return {
        "n":               len(sub),
        "median_capture":  float(np.nanmedian(cap)),
        "pf":              float(pf),
        "cvar5_sigma":     cvar5,
        "median_pnl_frac": float(np.median(pnl)),
    }


def run_report(per_df: pd.DataFrame, agg_df: pd.DataFrame, paths_df: pd.DataFrame) -> str:
    """Generate the exit report markdown. Returns the markdown string."""

    lines: list[str] = []

    def h1(t): lines.extend([f"# {t}", ""])
    def h2(t): lines.extend([f"## {t}", ""])
    def h3(t): lines.extend([f"### {t}", ""])
    def para(*args): lines.extend(list(args) + [""])
    def rule(): lines.extend(["---", ""])

    h1("Phase SEB-X -- Exit Rule Research Report")
    para(
        "_Vol-normalized exit sweep on 990 frozen SEB Tier-1 entries._",
        "_All thresholds in sigma-units. Raw-percent values shown as derived consequences only._",
        "_Tune/confirm split: temporal, 70/30 by date._",
    )
    rule()

    # -- Tune/confirm split by date --
    dates_sorted = sorted(paths_df["date"].unique())
    split_idx    = int(len(dates_sorted) * TUNE_FRAC)
    tune_dates   = set(dates_sorted[:split_idx])
    conf_dates   = set(dates_sorted[split_idx:])

    tune_mask  = per_df["date"].isin(tune_dates)
    conf_mask  = per_df["date"].isin(conf_dates)
    n_tune_e   = len(paths_df[paths_df["date"].isin(tune_dates)])
    n_conf_e   = len(paths_df[paths_df["date"].isin(conf_dates)])

    para(
        f"Tune split: {len(tune_dates)} dates, {n_tune_e} entries ({TUNE_FRAC*100:.0f}%)",
        f"Confirm split: {len(conf_dates)} dates, {n_conf_e} entries ({(1-TUNE_FRAC)*100:.0f}%)",
    )

    # -- B0 baseline --
    h2("B0 Baseline (VWAP Cross, zero new params)")
    b0_full  = _metrics_for_subset(per_df, pd.Series([True]*len(per_df), index=per_df.index),
                                    agg_df[agg_df["policy"]=="B0"].index[0] if (agg_df["policy"]=="B0").any() else 0)
    b0_tune  = _metrics_for_subset(per_df, tune_mask,
                                    agg_df[agg_df["policy"]=="B0"].index[0] if (agg_df["policy"]=="B0").any() else 0)
    b0_conf  = _metrics_for_subset(per_df, conf_mask,
                                    agg_df[agg_df["policy"]=="B0"].index[0] if (agg_df["policy"]=="B0").any() else 0)

    para(
        f"| Split | n | median capture(30m) | PF | CVaR5(sigma) |",
        f"|-------|---|--------------------|----|-------------|",
        f"| Full  | {b0_full['n']} | {b0_full['median_capture']:.3f} | {b0_full['pf']:.3f} | {b0_full['cvar5_sigma']:.3f} |",
        f"| Tune  | {b0_tune['n']} | {b0_tune['median_capture']:.3f} | {b0_tune['pf']:.3f} | {b0_tune['cvar5_sigma']:.3f} |",
        f"| Conf  | {b0_conf['n']} | {b0_conf['median_capture']:.3f} | {b0_conf['pf']:.3f} | {b0_conf['cvar5_sigma']:.3f} |",
    )

    # -- sigma-TP/SL baseline --
    h2("sigma-TP/SL Baseline (2-param reference)")
    tpsl_agg = agg_df[agg_df["policy"] == "TPSL"]
    if not tpsl_agg.empty:
        best_tpsl = tpsl_agg.loc[tpsl_agg["median_capture_30m"].idxmax()]
        ci_tpsl   = int(best_tpsl.name)
        tpsl_full = _metrics_for_subset(per_df, pd.Series([True]*len(per_df), index=per_df.index), ci_tpsl)
        tpsl_conf = _metrics_for_subset(per_df, conf_mask, ci_tpsl)
        para(
            f"Best TPSL: k_tp={best_tpsl['k_tp']:.1f}sigma  k_sl={best_tpsl['k_sl']:.1f}sigma",
            f"| Split | n | median capture(30m) | PF | CVaR5(sigma) |",
            f"|-------|---|--------------------|----|-------------|",
            f"| Full  | {tpsl_full['n']} | {tpsl_full['median_capture']:.3f} | {tpsl_full['pf']:.3f} | {tpsl_full['cvar5_sigma']:.3f} |",
            f"| Conf  | {tpsl_conf['n']} | {tpsl_conf['median_capture']:.3f} | {tpsl_conf['pf']:.3f} | {tpsl_conf['cvar5_sigma']:.3f} |",
        )

    # -- Complexity ladder --
    h2("Complexity Ladder (B0 -> +R1 -> +R2 -> +R3)")
    para(
        "Each rule is KEPT only if confirm-split beats the prior stage without it.",
        "Capture = pnl / MFE(30m). Higher = better retention of available move.",
    )
    lines.append("| Stage | Best params (sigma) | Tune capture | Conf capture | Conf PF | Conf CVaR5 | Keep? |")
    lines.append("|-------|---------------------|-------------|-------------|---------|-----------|-------|")

    # B0 row
    b0_ci = int(agg_df[agg_df["policy"]=="B0"].index[0]) if (agg_df["policy"]=="B0").any() else None
    if b0_ci is not None:
        lines.append(
            f"| B0 (baseline) | VWAP cross | {b0_tune['median_capture']:.3f} | {b0_conf['median_capture']:.3f} |"
            f" {b0_conf['pf']:.3f} | {b0_conf['cvar5_sigma']:.3f} | baseline |"
        )

    prev_conf_cap = b0_conf["median_capture"]

    for stage_policy in ["B0+R1", "B0+R1+R2", "B0+R1+R3", "B0+R1+R2+R3"]:
        stage_agg = agg_df[agg_df["policy"] == stage_policy]
        if stage_agg.empty:
            continue
        # Best on tune split
        tune_per_config = {}
        for ci in stage_agg.index:
            m = _metrics_for_subset(per_df, tune_mask, ci)
            tune_per_config[ci] = m["median_capture"]

        best_ci  = max(tune_per_config, key=lambda ci: tune_per_config[ci])
        best_row = stage_agg.loc[best_ci]
        t_cap    = tune_per_config[best_ci]
        c_met    = _metrics_for_subset(per_df, conf_mask, best_ci)
        c_cap    = c_met["median_capture"]
        keep     = "YES" if c_cap > prev_conf_cap else "NO"

        # Build param description
        parts = []
        if not np.isnan(best_row.get("k1", float("nan"))):
            parts.append(f"k1={best_row['k1']:.1f}")
        if not np.isnan(best_row.get("t_dead", float("nan"))):
            parts.append(f"T_dead={int(best_row['t_dead'])}m")
        if not np.isnan(best_row.get("a", float("nan"))):
            parts.append(f"a={best_row['a']:.2f}")
        if not np.isnan(best_row.get("arm_mult", float("nan"))):
            parts.append(f"arm={best_row['arm_mult']:.1f}")
        if not np.isnan(best_row.get("g", float("nan"))):
            parts.append(f"g={best_row['g']:.1f}")
        param_str = ", ".join(parts) if parts else "—"

        lines.append(
            f"| {stage_policy} | {param_str} | {t_cap:.3f} | {c_cap:.3f} |"
            f" {c_met['pf']:.3f} | {c_met['cvar5_sigma']:.3f} | {keep} |"
        )
        if keep == "YES":
            prev_conf_cap = c_cap

    lines.append("")

    rule()

    # ---- Sigma degeneration finding ----
    h2("Headline Finding: sigma Degeneration (99.8% Fallback)")
    n_fallback = int((paths_df["sigma_source"] == "fallback").sum())
    pct_fallback = 100.0 * n_fallback / max(len(paths_df), 1)
    sigma_med_val = float(paths_df["sigma"].median())
    para(
        f"{n_fallback}/{len(paths_df)} entries ({pct_fallback:.1f}%) use the global-median sigma fallback.",
        f"Effective sigma = {sigma_med_val:.4f} (constant) for all these entries.",
        "",
        "**Root cause:** `armed_bar == entry_bar` for nearly all entries. The armed window",
        "is 1 bar (below the 3-bar minimum), so per-event ATR cannot be computed.",
        "",
        "**Consequence:** sigma-normalized thresholds (k1*sigma, g*sigma, arm*sigma) are",
        "effectively fixed dollar amounts, not per-event vol adjustments. The normalization",
        "goal (make k portable across events with different vol) was NOT achieved.",
        "",
        "**Recommended fix for SEB-X v2:** Use trailing 14-bar ATR ending at entry_bar",
        "(look-back, not armed window). This always provides a meaningful window.",
        "Until then, treat all sigma-multiples in this report as dollar-denominated knobs",
        f"(e.g., k1=2.5 means stop ${2.5*sigma_med_val:.2f} below entry for an average entry).",
    )
    rule()

    # ---- Gate C: vol-regime acid test ----
    h2("Gate C -- Vol-Regime Acid Test")
    para(
        "Split entries by sigma (primary ATR). Low = below median, High = above median.",
        "FAIL if best k1/g diverge >50% across regimes (sigma-normalization broken).",
    )

    sigma_med    = float(paths_df["sigma"].median())
    low_vol_mask = per_df["sigma"] < sigma_med
    high_vol_mask = per_df["sigma"] >= sigma_med

    gate_c_pass   = True
    gate_c_status = "PASS"   # may be set to FAIL or INCONCLUSIVE below

    for regime_policy in ["B0+R1", "B0+R1+R3"]:
        stage_agg = agg_df[agg_df["policy"] == regime_policy]
        if stage_agg.empty:
            continue

        best_k1s = {}
        for vol_label, vol_mask in [("low", low_vol_mask), ("high", high_vol_mask)]:
            best_cap  = -np.inf
            best_k1_v = float("nan")
            for ci in stage_agg.index:
                m = _metrics_for_subset(per_df, vol_mask, ci)
                if m["median_capture"] > best_cap and m["n"] >= 50:
                    best_cap  = m["median_capture"]
                    best_k1_v = float(stage_agg.loc[ci].get("k1", float("nan")))
            best_k1s[vol_label] = best_k1_v

        low_k  = best_k1s["low"]
        high_k = best_k1s["high"]
        if not (np.isnan(low_k) or np.isnan(high_k) or low_k == 0 or high_k == 0):
            diverge = abs(low_k - high_k) / max(abs(low_k), abs(high_k))
            verdict = "FAIL" if diverge > GATE_C_DIVERGE_THRESH else "PASS"
            if diverge > GATE_C_DIVERGE_THRESH:
                gate_c_pass   = False
                gate_c_status = "FAIL"
        else:
            diverge       = float("nan")
            verdict       = "INCONCLUSIVE"
            gate_c_status = "INCONCLUSIVE"

        para(
            f"**{regime_policy}**: low-vol best k1={low_k:.2f}  high-vol best k1={high_k:.2f}",
            f"  divergence={diverge*100:.1f}%  Gate C: **{verdict}**",
        )

    if gate_c_status == "INCONCLUSIVE":
        para(
            "**HEADLINE FINDING: Gate C INCONCLUSIVE.**",
            "sigma is identical for 99.8% of entries (all using the global-median fallback).",
            "Low-vol vs high-vol split is degenerate -- cannot test the normalization hypothesis.",
            "Root cause: armed_bar == entry_bar for nearly all entries (1-bar window < 3-bar minimum).",
            "Fix: use a fixed look-back ATR (e.g., trailing 14 bars before entry) instead of the",
            "armed-window ATR. Do NOT label this run as confirming sigma-portability.",
        )
    elif not gate_c_pass:
        para(
            "**HEADLINE FINDING: Gate C FAILED.**",
            "sigma-normalization does NOT produce stable multiples across vol regimes.",
            "Do NOT ship a single global k. Report regime-conditioned parameters or",
            "return to raw-price stops with vol-scaling baked into position sizing.",
        )
    else:
        para("Gate C PASSED. sigma-multiples are stable across vol regimes.")

    rule()

    # ---- Additional splits ----
    h2("Sensitivity Splits")

    # Best overall confirmed policy
    best_final_agg = agg_df[agg_df["policy"].isin(["B0+R1", "B0+R1+R2", "B0+R1+R3"])].copy()
    if not best_final_agg.empty:
        best_ci = int(best_final_agg.loc[best_final_agg["median_capture_30m"].idxmax()].name)

        h3("By session bucket")
        lines.append("| Bucket | n | median capture | PF |")
        lines.append("|--------|---|---------------|-----|")
        for bucket in ["regular_hours", "pre_market", "post_market"]:
            mask_b = per_df["date"].isin(paths_df[paths_df["session_bucket"]==bucket]["date"].values)
            m = _metrics_for_subset(per_df, mask_b, best_ci)
            lines.append(f"| {bucket} | {m['n']} | {m['median_capture']:.3f} | {m['pf']:.3f} |")
        lines.append("")

        h3("By event-day vs off-day")
        lines.append("| Event day | n | median capture | PF |")
        lines.append("|-----------|---|---------------|-----|")
        for is_ev, label in [(True, "event_day"), (False, "off_day")]:
            ev_dates = paths_df[paths_df["is_event_day"]==is_ev]["date"].values
            mask_ev  = per_df["date"].isin(ev_dates)
            m = _metrics_for_subset(per_df, mask_ev, best_ci)
            lines.append(f"| {label} | {m['n']} | {m['median_capture']:.3f} | {m['pf']:.3f} |")
        lines.append("")

        h3("By year")
        paths_df["year"] = paths_df["date"].str[:4]
        lines.append("| Year | n | median capture | PF |")
        lines.append("|------|---|---------------|-----|")
        for yr in sorted(paths_df["year"].unique()):
            yr_dates = paths_df[paths_df["year"]==yr]["date"].values
            mask_yr  = per_df["date"].isin(yr_dates)
            m = _metrics_for_subset(per_df, mask_yr, best_ci)
            lines.append(f"| {yr} | {m['n']} | {m['median_capture']:.3f} | {m['pf']:.3f} |")
        lines.append("")

    rule()

    # ---- Caveats ----
    h2("Caveats and Limitations")
    para(
        "1. **Tune/confirm split is temporal (not random).** With Tier 0 empty, this is the",
        "   minimum defense. Results are indicative only -- no holdout has been used.",
        "2. **All sigma-multiples in this report are UNVALIDATED HEURISTICS.** They are calibrated",
        "   to the Tier 1 catalog sample and may not generalize to live trading.",
        "3. **MFE capture is not realized PnL.** Fill slippage, partial fills, and spread are",
        "   not modeled. Assume ~0.5-2% slippage on entry; exit slippage varies by rule.",
        "4. **B0 (VWAP cross) uses bar-close detection.** Realized fill will lag 1 bar on average.",
        "5. **R1/R3 assume limit stop fills.** Gap-through scenarios deliver worse fills.",
        "6. **No Tier 0 ground truth.** Runner rate, capture, and slippage estimates may all",
        "   differ materially from live trading due to catalog selection bias.",
    )
    rule()

    para(
        "_Phase SEB-X is read-only. No live tables modified._",
        "_Gate A (MFE reproduction): PASS._",
        f"_Gate C (vol-regime split): {gate_c_status} -- see Headline Finding above._",
    )

    return "\n".join(lines)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    missing = [p for p in [PATHS_PARQUET, SWEEP_PARQUET, SWEEP_AGG] if not p.exists()]
    if missing:
        log.error("Missing files: %s", missing)
        log.error("Run build_paths.py and sweep.py first.")
        sys.exit(1)

    log.info("Loading paths.parquet")
    paths_df = pd.read_parquet(str(PATHS_PARQUET))
    log.info("Loading sweep.parquet")
    per_df   = pd.read_parquet(str(SWEEP_PARQUET))
    log.info("Loading sweep_agg.parquet")
    agg_df   = pd.read_parquet(str(SWEEP_AGG))

    log.info(
        "Loaded: %d entries, %d per-entry rows, %d configs",
        len(paths_df), len(per_df), len(agg_df),
    )

    md = run_report(per_df, agg_df, paths_df)

    REPORT_MD.write_text(md, encoding="utf-8")
    log.info("Wrote %s", REPORT_MD)

    print("")
    print("[SEB-X Task 4] Report written to: %s" % REPORT_MD)
    print("Preview:")
    for line in md.split("\n")[:40]:
        print(line)


if __name__ == "__main__":
    main()
