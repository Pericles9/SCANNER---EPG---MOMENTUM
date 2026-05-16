"""Phase F aggregate diagnostic charts (T6c).

Reads per_trade.parquet from a results split and writes 8 standalone HTML charts
to results/phase_f/charts_{split}/.

Charts:
    1. equity_curve.html     -- cumulative PnL% path ordered by exit timestamp
    2. returns_kde.html      -- kernel density estimate of trade pnl_pct
    3. exit_breakdown.html   -- count, mean_pnl%, PF by exit_reason
    4. event_pnl_ranked.html -- per-event cumulative PnL% ranked bar
    5. pf_by_month.html      -- monthly profit factor
    6. hold_distribution.html -- hold_sec histogram by exit_reason
    7. pnl_scatter.html      -- pnl_pct vs hold_sec scatter (exit_reason color)
    8. entry_type_compare.html -- first vs reentry PnL distribution comparison

Usage:
    python -m tools.phase_f.aggregate_charts --split val_full
    python -m tools.phase_f.aggregate_charts --split val_sample
"""
from __future__ import annotations

import argparse
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import scipy.stats as stats

_ROOT = Path(__file__).resolve().parents[2]
_PHASE_F_BASE = _ROOT / "results" / "phase_f"

_EXIT_COLORS = {
    "luld_upper": "#FF6600",
    "exit_d": "#3366FF",
    "epg_window_close": "#00AA44",
    "luld_lower": "#CC0000",
}
_ENTRY_COLORS = {"first": "#00CC44", "reentry": "#3399FF"}

_CHART_W = 1200
_CHART_H = 600


def _pf(wins_sum: float, losses_sum: float) -> float:
    return wins_sum / losses_sum if losses_sum > 0 else float("nan")


def _exit_color(reason: str) -> str:
    return _EXIT_COLORS.get(reason, "#888888")


def _chart1_equity_curve(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    d = df.sort_values("exit_ts").copy()
    d["cum_pnl"] = d["pnl_pct"].cumsum()
    d["trade_num"] = np.arange(1, len(d) + 1)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=d["trade_num"], y=d["cum_pnl"],
        mode="lines",
        line=dict(color="#3366FF", width=2),
        name="Cumulative PnL%",
        hovertemplate="Trade %{x}<br>Cum PnL: %{y:.3f}%<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1))

    fig.update_layout(
        title=f"Phase F ({split}) -- Equity Curve (cumulative PnL%)",
        xaxis_title="Trade sequence (sorted by exit timestamp)",
        yaxis_title="Cumulative PnL%",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "equity_curve.html"), include_plotlyjs="cdn")
    print(f"  [1/8] equity_curve.html  ({len(d)} trades)")


def _chart2_returns_kde(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    pnl = df["pnl_pct"].values
    xs = np.linspace(pnl.min() - 1, pnl.max() + 1, 500)
    kde = stats.gaussian_kde(pnl, bw_method="scott")
    ys = kde(xs)

    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=xs, y=ys,
        mode="lines",
        fill="tozeroy",
        line=dict(color="#3366FF", width=2),
        fillcolor="rgba(51,102,255,0.15)",
        name="KDE",
        hovertemplate="PnL: %{x:.3f}%<br>Density: %{y:.4f}<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="#CC2200", dash="dash", width=1.5))
    fig.add_vline(x=float(np.mean(pnl)),
                  line=dict(color="#00AA44", dash="dot", width=1.5),
                  annotation_text=f"mean={np.mean(pnl):.3f}%",
                  annotation_position="top right")

    fig.update_layout(
        title=f"Phase F ({split}) -- Trade Returns KDE (n={len(pnl)})",
        xaxis_title="PnL%",
        yaxis_title="Density",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "returns_kde.html"), include_plotlyjs="cdn")
    print(f"  [2/8] returns_kde.html  ({len(pnl)} trades)")


def _chart3_exit_breakdown(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    reasons = df["exit_reason"].unique()
    rows = []
    for r in sorted(reasons):
        sub = df[df["exit_reason"] == r]
        wins = sub.loc[sub["pnl_pct"] > 0, "pnl_pct"].sum()
        losses = abs(sub.loc[sub["pnl_pct"] < 0, "pnl_pct"].sum())
        pf = _pf(wins, losses)
        rows.append({
            "reason": r,
            "count": len(sub),
            "mean_pnl": sub["pnl_pct"].mean(),
            "pf": pf,
            "color": _exit_color(r),
        })
    rdf = pd.DataFrame(rows)

    fig = make_subplots(rows=1, cols=3, subplot_titles=["Count", "Mean PnL%", "Profit Factor"])

    for _, row in rdf.iterrows():
        fig.add_trace(go.Bar(
            x=[row["reason"]], y=[row["count"]],
            marker_color=row["color"],
            name=row["reason"], legendgroup=row["reason"],
            hovertemplate=f'{row["reason"]}<br>count=%{{y}}<extra></extra>',
        ), row=1, col=1)
        fig.add_trace(go.Bar(
            x=[row["reason"]], y=[row["mean_pnl"]],
            marker_color=row["color"],
            name=row["reason"], legendgroup=row["reason"], showlegend=False,
            hovertemplate=f'{row["reason"]}<br>mean_pnl=%{{y:.3f}}%<extra></extra>',
        ), row=1, col=2)
        pf_val = row["pf"] if not np.isnan(row["pf"]) else 0.0
        fig.add_trace(go.Bar(
            x=[row["reason"]], y=[pf_val],
            marker_color=row["color"],
            name=row["reason"], legendgroup=row["reason"], showlegend=False,
            hovertemplate=f'{row["reason"]}<br>PF=%{{y:.4f}}<extra></extra>',
        ), row=1, col=3)

    fig.update_layout(
        title=f"Phase F ({split}) -- Exit Reason Breakdown",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
        barmode="group",
    )
    fig.write_html(str(out_dir / "exit_breakdown.html"), include_plotlyjs="cdn")
    print(f"  [3/8] exit_breakdown.html  ({len(reasons)} exit reasons)")


def _chart4_event_pnl_ranked(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    event_pnl = (
        df.groupby(["ticker", "date"])["pnl_pct"]
        .sum()
        .reset_index()
        .sort_values("pnl_pct", ascending=False)
        .reset_index(drop=True)
    )
    event_pnl["label"] = event_pnl["ticker"] + " " + event_pnl["date"]
    colors = ["#00AA44" if p > 0 else "#CC2200" for p in event_pnl["pnl_pct"]]

    fig = go.Figure(go.Bar(
        x=event_pnl.index,
        y=event_pnl["pnl_pct"],
        marker_color=colors,
        customdata=np.column_stack([event_pnl["label"]]),
        hovertemplate="%{customdata[0]}<br>cum_pnl=%{y:.3f}%<extra></extra>",
    ))
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1))

    fig.update_layout(
        title=f"Phase F ({split}) -- Event Cumulative PnL% (ranked, n={len(event_pnl)} events)",
        xaxis_title="Event rank",
        yaxis_title="Cumulative PnL%",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "event_pnl_ranked.html"), include_plotlyjs="cdn")
    print(f"  [4/8] event_pnl_ranked.html  ({len(event_pnl)} events)")


def _chart5_pf_by_month(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    d = df.copy()
    d["exit_month"] = pd.to_datetime(d["date"]).dt.to_period("M").astype(str)
    months = sorted(d["exit_month"].unique())

    pf_vals, labels = [], []
    for m in months:
        sub = d[d["exit_month"] == m]
        wins = sub.loc[sub["pnl_pct"] > 0, "pnl_pct"].sum()
        losses = abs(sub.loc[sub["pnl_pct"] < 0, "pnl_pct"].sum())
        pf = _pf(wins, losses)
        pf_vals.append(pf if not np.isnan(pf) else 0.0)
        labels.append(m)

    colors = ["#00AA44" if p >= 1.0 else "#CC2200" for p in pf_vals]

    fig = go.Figure(go.Bar(
        x=labels, y=pf_vals,
        marker_color=colors,
        hovertemplate="Month: %{x}<br>PF=%{y:.4f}<extra></extra>",
    ))
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1),
                  annotation_text="PF=1.0", annotation_position="bottom right")

    fig.update_layout(
        title=f"Phase F ({split}) -- Profit Factor by Month",
        xaxis_title="Month",
        yaxis_title="Profit Factor",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "pf_by_month.html"), include_plotlyjs="cdn")
    print(f"  [5/8] pf_by_month.html  ({len(months)} months)")


def _chart6_hold_distribution(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    fig = go.Figure()
    reasons = sorted(df["exit_reason"].unique())
    for reason in reasons:
        sub = df[df["exit_reason"] == reason]
        fig.add_trace(go.Histogram(
            x=sub["hold_sec"],
            name=reason,
            opacity=0.7,
            marker_color=_exit_color(reason),
            nbinsx=50,
            hovertemplate=f"{reason}<br>hold_sec=%{{x}}<br>count=%{{y}}<extra></extra>",
        ))

    fig.update_layout(
        title=f"Phase F ({split}) -- Hold Time Distribution by Exit Reason (n={len(df)})",
        xaxis_title="Hold time (seconds)",
        yaxis_title="Count",
        barmode="overlay",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "hold_distribution.html"), include_plotlyjs="cdn")
    print(f"  [6/8] hold_distribution.html  ({len(reasons)} exit reasons)")


def _chart7_pnl_scatter(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    fig = go.Figure()
    reasons = sorted(df["exit_reason"].unique())
    for reason in reasons:
        sub = df[df["exit_reason"] == reason]
        fig.add_trace(go.Scatter(
            x=sub["hold_sec"],
            y=sub["pnl_pct"],
            mode="markers",
            name=reason,
            marker=dict(
                color=_exit_color(reason),
                size=6,
                opacity=0.6,
                line=dict(width=0.5, color="#222222"),
            ),
            hovertemplate=(
                f"{reason}<br>hold=%{{x:.1f}}s<br>pnl=%{{y:.3f}}%<extra></extra>"
            ),
        ))
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1))

    fig.update_layout(
        title=f"Phase F ({split}) -- PnL% vs Hold Time (colored by exit reason, n={len(df)})",
        xaxis_title="Hold time (seconds)",
        yaxis_title="PnL%",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "pnl_scatter.html"), include_plotlyjs="cdn")
    print(f"  [7/8] pnl_scatter.html  ({len(df)} trades)")


def _chart8_entry_type_compare(df: pd.DataFrame, split: str, out_dir: Path) -> None:
    fig = make_subplots(rows=1, cols=2,
                        subplot_titles=["PnL% Distribution by Entry Type",
                                        "Profit Factor by Entry Type"])

    for etype, color in _ENTRY_COLORS.items():
        sub = df[df["entry_type"] == etype]
        if len(sub) == 0:
            continue
        fig.add_trace(go.Box(
            y=sub["pnl_pct"],
            name=etype,
            marker_color=color,
            boxmean=True,
            hovertemplate=f"{etype}<br>pnl=%{{y:.3f}}%<extra></extra>",
        ), row=1, col=1)

    entry_pfs = []
    for etype, color in _ENTRY_COLORS.items():
        sub = df[df["entry_type"] == etype]
        if len(sub) == 0:
            continue
        wins = sub.loc[sub["pnl_pct"] > 0, "pnl_pct"].sum()
        losses = abs(sub.loc[sub["pnl_pct"] < 0, "pnl_pct"].sum())
        pf = _pf(wins, losses)
        entry_pfs.append((etype, pf if not np.isnan(pf) else 0.0, color))

    for etype, pf_val, color in entry_pfs:
        fig.add_trace(go.Bar(
            x=[etype], y=[pf_val],
            marker_color=color,
            name=etype, showlegend=False,
            hovertemplate=f"{etype}<br>PF=%{{y:.4f}}<extra></extra>",
        ), row=1, col=2)

    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1),
                  row=1, col=2)

    fig.update_layout(
        title=f"Phase F ({split}) -- Entry Type Comparison (n={len(df)} trades)",
        width=_CHART_W, height=_CHART_H,
        template="plotly_white",
    )
    fig.write_html(str(out_dir / "entry_type_compare.html"), include_plotlyjs="cdn")
    print(f"  [8/8] entry_type_compare.html  ({df['entry_type'].value_counts().to_dict()})")


def main(split: str) -> None:
    results_dir = _PHASE_F_BASE / split
    out_dir = _PHASE_F_BASE / f"charts_{split}"

    per_trade_path = results_dir / "per_trade.parquet"
    if not per_trade_path.exists():
        print(f"ERROR: {per_trade_path} not found -- run backtest first")
        sys.exit(1)

    out_dir.mkdir(parents=True, exist_ok=True)

    df = pd.read_parquet(per_trade_path)
    print(f"Phase F aggregate charts -- split={split} -- {len(df)} trades -> {out_dir}/")
    print()

    _chart1_equity_curve(df, split, out_dir)
    _chart2_returns_kde(df, split, out_dir)
    _chart3_exit_breakdown(df, split, out_dir)
    _chart4_event_pnl_ranked(df, split, out_dir)
    _chart5_pf_by_month(df, split, out_dir)
    _chart6_hold_distribution(df, split, out_dir)
    _chart7_pnl_scatter(df, split, out_dir)
    _chart8_entry_type_compare(df, split, out_dir)

    print(f"\nAll 8 charts written to {out_dir}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Phase F aggregate diagnostic charts")
    parser.add_argument(
        "--split",
        required=True,
        choices=["val_full", "val_sample", "test_full"],
    )
    args = parser.parse_args()
    main(split=args.split)
