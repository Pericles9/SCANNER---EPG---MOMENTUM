"""
Phase SEB-X v2-VIZ Task 2 -- Full distribution metrics on realized per-trade PnL.

Primary metric: realized_ret_pct (% return). Secondary: realized_ret_sigma (dollar PnL / sigma).

Gate C: compare sign(EV/mean) vs sign(median_capture). If they disagree, flag as headline.

Splits: overall, by year, by session_bucket, event-day vs off-day.

Outputs:
  results/seb_x_v2viz/metrics_v2viz.md   (markdown tables)
  results/seb_x_v2viz/metrics_v2viz.csv  (machine-readable)
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

log = logging.getLogger(__name__)

_REPO_ROOT  = Path(__file__).resolve().parents[2]
OUTPUT_DIR  = _REPO_ROOT / "results" / "seb_x_v2viz"
IN_PARQUET  = OUTPUT_DIR / "per_trade_exits.parquet"
OUT_MD      = OUTPUT_DIR / "metrics_v2viz.md"
OUT_CSV     = OUTPUT_DIR / "metrics_v2viz.csv"


def _dist_metrics(series: np.ndarray, label: str) -> dict:
    """Full distribution metrics for a PnL series."""
    valid = series[~np.isnan(series)]
    n     = len(valid)
    if n == 0:
        return {"label": label, "n": 0}

    wins   = valid[valid > 0]
    losses = valid[valid < 0]

    ev     = float(np.mean(valid))
    std    = float(np.std(valid, ddof=1)) if n > 1 else 0.0
    q1, med, q3 = (float(v) for v in np.percentile(valid, [25, 50, 75]))

    pct_win  = float(len(wins) / n)
    avg_win  = float(np.mean(wins))  if len(wins)  > 0 else 0.0
    avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0.0

    payoff = abs(avg_win / avg_loss) if avg_loss < 0 else float("inf")
    expectancy = pct_win * avg_win + (1.0 - pct_win) * avg_loss

    gross_win  = float(wins.sum())       if len(wins)  > 0 else 0.0
    gross_loss = float(abs(losses.sum())) if len(losses) > 0 else 0.0
    pf         = gross_win / gross_loss   if gross_loss > 0  else float("inf")

    # CVaR5: mean of worst 5%
    if n >= 20:
        cut   = float(np.percentile(valid, 5))
        cvar5 = float(np.mean(valid[valid <= cut]))
    else:
        cvar5 = float("nan")

    skew = float(stats.skew(valid))
    kurt = float(stats.kurtosis(valid, fisher=True))

    # Max consecutive losers
    cur_streak = max_streak = 0
    for v in valid:
        if v <= 0:
            cur_streak += 1
            max_streak = max(max_streak, cur_streak)
        else:
            cur_streak = 0

    # Cumulative max drawdown
    cum = np.cumsum(valid)
    running_max = np.maximum.accumulate(cum)
    max_dd = float(np.max(running_max - cum)) if n > 0 else 0.0

    return {
        "label":             label,
        "n":                 n,
        "ev":                round(ev,   5),
        "std":               round(std,  5),
        "min":               round(float(np.min(valid)), 5),
        "q1":                round(q1,   5),
        "median":            round(med,  5),
        "q3":                round(q3,   5),
        "max":               round(float(np.max(valid)), 5),
        "iqr":               round(q3 - q1, 5),
        "skew":              round(skew, 3),
        "kurtosis":          round(kurt, 3),
        "pct_win":           round(pct_win, 4),
        "avg_win":           round(avg_win,  5),
        "avg_loss":          round(avg_loss, 5),
        "payoff_ratio":      round(payoff, 3),
        "expectancy":        round(expectancy, 5),
        "profit_factor":     round(pf, 4),
        "cvar5":             round(cvar5, 5) if not math.isnan(cvar5) else float("nan"),
        "max_consec_loss":   int(max_streak),
        "max_drawdown_sum":  round(max_dd, 5),
        "ev_over_std":       round(ev / std, 4) if std > 0 else float("nan"),
    }


def _fmt_pct(v: float, decimals: int = 2) -> str:
    if math.isnan(v):
        return "nan"
    return f"{v * 100:.{decimals}f}%"


def _fmt_f(v: float, decimals: int = 3) -> str:
    if math.isnan(v):
        return "nan"
    return f"{v:.{decimals}f}"


def compute_metrics(df: pd.DataFrame) -> tuple[pd.DataFrame, list[dict]]:
    """Compute distribution metrics across all splits. Returns (metrics_df, gate_c_findings)."""
    records: list[dict] = []

    stacks = df["stack"].unique()
    for stack in stacks:
        sub = df[df["stack"] == stack]

        def _add(label: str, rows: pd.DataFrame) -> None:
            if len(rows) == 0:
                return
            pct_arr   = rows["realized_ret_pct"].values.astype(np.float64)
            sigma_arr = rows["realized_ret_sigma"].values.astype(np.float64)
            cap_arr   = rows["capture30"].values.astype(np.float64)
            base = {"stack": stack, "split_type": "overall", "split_val": label}
            m_pct   = _dist_metrics(pct_arr,   f"{stack}|{label}|pct")
            m_sigma = _dist_metrics(sigma_arr, f"{stack}|{label}|sigma")
            m_cap   = _dist_metrics(cap_arr,   f"{stack}|{label}|cap")
            records.append({
                **base,
                # raw %
                "n":             m_pct["n"],
                "ev_pct":        m_pct["ev"],
                "std_pct":       m_pct["std"],
                "min_pct":       m_pct["min"],
                "q1_pct":        m_pct["q1"],
                "median_pct":    m_pct["median"],
                "q3_pct":        m_pct["q3"],
                "max_pct":       m_pct["max"],
                "iqr_pct":       m_pct["iqr"],
                "skew":          m_pct["skew"],
                "kurtosis":      m_pct["kurtosis"],
                "pct_win":       m_pct["pct_win"],
                "avg_win_pct":   m_pct["avg_win"],
                "avg_loss_pct":  m_pct["avg_loss"],
                "payoff_ratio":  m_pct["payoff_ratio"],
                "expectancy_pct": m_pct["expectancy"],
                "profit_factor": m_pct["profit_factor"],
                "cvar5_pct":     m_pct["cvar5"],
                "max_consec_loss": m_pct["max_consec_loss"],
                "max_drawdown_pct": m_pct["max_drawdown_sum"],
                "ev_over_std":   m_pct["ev_over_std"],
                # sigma-unit secondary
                "ev_sigma":      m_sigma["ev"],
                "cvar5_sigma":   m_sigma["cvar5"],
                "median_sigma":  m_sigma["median"],
                # capture
                "ev_cap":        m_cap["ev"],
                "median_cap":    m_cap["median"],
            })

        _add("overall", sub)

        sub2 = sub.copy()
        sub2["split_type"] = "year"
        for yr in sorted(sub["year"].unique()):
            _add(str(yr), sub[sub["year"] == yr])

        for bkt in sorted(sub["session_bucket"].dropna().unique()):
            _add(bkt, sub[sub["session_bucket"] == bkt])

        _add("event_day",    sub[sub["is_event_day"] == True])
        _add("non_event_day", sub[sub["is_event_day"] == False])

    metrics_df = pd.DataFrame(records)

    # Gate C: sign check EV vs median capture for each stack (overall split)
    gate_c: list[dict] = []
    overall = metrics_df[metrics_df["split_val"] == "overall"]
    for _, row in overall.iterrows():
        ev_sign     = "+" if row["ev_pct"] > 0 else "-"
        median_sign = "+" if row["median_pct"] > 0 else "-"
        agree       = ev_sign == median_sign
        gate_c.append({
            "stack":       row["stack"],
            "ev_pct":      row["ev_pct"],
            "median_pct":  row["median_pct"],
            "ev_cap":      row["ev_cap"],
            "median_cap":  row["median_cap"],
            "agree":       agree,
            "flag":        "" if agree else "*** EV/MEDIAN DISAGREE — fat left tail",
        })
        log.info(
            "Gate C [%s]: EV=%s%s  median=%s%s  capture EV=%s%s  capture median=%s%s  %s",
            row["stack"],
            ev_sign, _fmt_pct(row["ev_pct"]),
            median_sign, _fmt_pct(row["median_pct"]),
            "+" if row["ev_cap"] > 0 else "-", _fmt_f(row["ev_cap"]),
            "+" if row["median_cap"] > 0 else "-", _fmt_f(row["median_cap"]),
            "AGREE" if agree else "*** DISAGREE",
        )

    return metrics_df, gate_c


def _render_md(df: pd.DataFrame, gate_c: list[dict]) -> str:
    lines = ["# Phase SEB-X v2-VIZ — Distribution Metrics", ""]

    # Gate C headline
    disagrees = [g for g in gate_c if not g["agree"]]
    if disagrees:
        lines += [
            "## *** GATE C: EV / MEDIAN SIGN DISAGREEMENT (fat-left-tail signature) ***",
            "",
        ]
        for g in disagrees:
            lines.append(
                f"Stack **{g['stack']}**: EV={_fmt_pct(g['ev_pct'])} but median={_fmt_pct(g['median_pct'])} "
                f"| EV capture={_fmt_f(g['ev_cap'])} median capture={_fmt_f(g['median_cap'])}"
            )
            lines.append("")
        lines += [
            "Interpretation: a minority of large losers is dragging mean below zero while the "
            "median trade is still profitable. This is the fat-left-tail signature. "
            "See left-tail contributors in the by-year and by-bucket splits below.",
            "",
        ]
    else:
        lines += [
            "## Gate C: EV / Median Agree (no fat-tail signature at aggregate level)",
            "",
        ]
        for g in gate_c:
            lines.append(
                f"Stack **{g['stack']}**: EV={_fmt_pct(g['ev_pct'])} median={_fmt_pct(g['median_pct'])} "
                f"| EV capture={_fmt_f(g['ev_cap'])} median capture={_fmt_f(g['median_cap'])} → AGREE"
            )
        lines.append("")

    # Per-stack overall tables
    for stack in df["stack"].unique():
        lines += [f"## Stack: {stack}", ""]
        sub = df[df["stack"] == stack]

        # Overall summary
        ov = sub[sub["split_val"] == "overall"].iloc[0]
        lines += [
            "### Overall",
            "",
            f"n={int(ov['n'])}  "
            f"EV={_fmt_pct(ov['ev_pct'])}  "
            f"median={_fmt_pct(ov['median_pct'])}  "
            f"std={_fmt_pct(ov['std_pct'])}  "
            f"skew={ov['skew']:.2f}  "
            f"kurt={ov['kurtosis']:.2f}",
            "",
            f"min={_fmt_pct(ov['min_pct'])}  "
            f"Q1={_fmt_pct(ov['q1_pct'])}  "
            f"Q3={_fmt_pct(ov['q3_pct'])}  "
            f"max={_fmt_pct(ov['max_pct'])}  "
            f"IQR={_fmt_pct(ov['iqr_pct'])}",
            "",
            f"%win={ov['pct_win']*100:.1f}%  "
            f"avg_win={_fmt_pct(ov['avg_win_pct'])}  "
            f"avg_loss={_fmt_pct(ov['avg_loss_pct'])}  "
            f"payoff={ov['payoff_ratio']:.3f}  "
            f"expectancy={_fmt_pct(ov['expectancy_pct'])}",
            "",
            f"PF={ov['profit_factor']:.4f}  "
            f"CVaR5_pct={_fmt_pct(ov['cvar5_pct'])}  "
            f"CVaR5_sigma={_fmt_f(ov['cvar5_sigma'])}  "
            f"max_consec_loss={int(ov['max_consec_loss'])}  "
            f"max_DD_sum={_fmt_pct(ov['max_drawdown_pct'])}  "
            f"EV/std={_fmt_f(ov['ev_over_std'])}",
            "",
        ]

        # By-year table
        year_rows = sub[sub["split_val"].str.match(r"^\d{4}$")].sort_values("split_val")
        if not year_rows.empty:
            lines += ["### By Year", ""]
            lines.append(
                "| Year | n | EV% | Median% | %win | avg_win | avg_loss | PF | CVaR5% |"
            )
            lines.append("|------|---|-----|---------|------|---------|----------|-----|--------|")
            for _, r in year_rows.iterrows():
                lines.append(
                    f"| {r['split_val']} | {int(r['n'])} | {_fmt_pct(r['ev_pct'])} | "
                    f"{_fmt_pct(r['median_pct'])} | {r['pct_win']*100:.1f}% | "
                    f"{_fmt_pct(r['avg_win_pct'])} | {_fmt_pct(r['avg_loss_pct'])} | "
                    f"{r['profit_factor']:.3f} | {_fmt_pct(r['cvar5_pct'])} |"
                )
            lines.append("")

        # By session bucket
        bkt_vals = {"regular_hours", "pre_market", "post_market"}
        bkt_rows = sub[sub["split_val"].isin(bkt_vals)].sort_values("split_val")
        if not bkt_rows.empty:
            lines += ["### By Session Bucket", ""]
            lines.append(
                "| Bucket | n | EV% | Median% | %win | PF | CVaR5% |"
            )
            lines.append("|--------|---|-----|---------|------|-----|--------|")
            for _, r in bkt_rows.iterrows():
                lines.append(
                    f"| {r['split_val']} | {int(r['n'])} | {_fmt_pct(r['ev_pct'])} | "
                    f"{_fmt_pct(r['median_pct'])} | {r['pct_win']*100:.1f}% | "
                    f"{r['profit_factor']:.3f} | {_fmt_pct(r['cvar5_pct'])} |"
                )
            lines.append("")

        # Event vs non-event
        ev_rows = sub[sub["split_val"].isin(["event_day", "non_event_day"])].sort_values("split_val")
        if not ev_rows.empty:
            lines += ["### Event Day vs Off-Day", ""]
            lines.append("| Split | n | EV% | Median% | PF |")
            lines.append("|-------|---|-----|---------|-----|")
            for _, r in ev_rows.iterrows():
                lines.append(
                    f"| {r['split_val']} | {int(r['n'])} | "
                    f"{_fmt_pct(r['ev_pct'])} | {_fmt_pct(r['median_pct'])} | "
                    f"{r['profit_factor']:.3f} |"
                )
            lines.append("")

    lines += [
        "---",
        "",
        "## Caveats",
        "",
        "1. **realized_ret_pct** = (exit_price - entry_price) / entry_price — no slippage/spread.",
        "2. **realized_ret_sigma** = dollar_PnL / sigma_val (consistent with stop calibration).",
        "   Note: v2 sweep.parquet stored pnl_frac/sigma_dollar (different unit) — CVaR5 there is not comparable.",
        "3. **Tier 0 empty** — loss distributions under-weight real faders; left tail likely thinner here than live.",
        "4. **No true holdout.** Tune/confirm split is temporal 70/30 on the same 990 entries.",
    ]

    return "\n".join(lines)


def main() -> pd.DataFrame:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    df = pd.read_parquet(str(IN_PARQUET))
    log.info("Loaded per_trade_exits: %d rows", len(df))

    metrics_df, gate_c = compute_metrics(df)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    metrics_df.to_csv(str(OUT_CSV), index=False)
    log.info("Wrote %s", OUT_CSV)

    md = _render_md(metrics_df, gate_c)
    OUT_MD.write_text(md, encoding="utf-8")
    log.info("Wrote %s", OUT_MD)

    return metrics_df


if __name__ == "__main__":
    main()
