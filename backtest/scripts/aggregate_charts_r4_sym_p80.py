#!/usr/bin/env python3
"""
Phase R1-Final — Aggregate diagnostic charts for sym_p80 on val_r4_stratified.

Charts (Plotly HTML, dark mode):
    1. pnl_kde_by_session.html   -- KDE of PnL% by session bucket (RTH / pre / post)
    2. cum_pnl_by_tod.html       -- Cumulative PnL% sorted by time-of-day of entry
    3. cum_pnl_by_stratum.html   -- Equity curve per stratum (low / mid / high)

Source:  phase_r1_final/sym_p80/per_trade.json
         backtest/data/val_r4_stratified.json  (stratum lookup)
Output:  phase_r1_final/event_charts_sym_p80/aggregate/
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import scipy.stats as spstats

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
BACKTEST     = PROJECT_ROOT / "backtest"
RESULTS      = BACKTEST / "results"

PER_TRADE = RESULTS / "phase_r1_final" / "sym_p80" / "per_trade.json"
SAMPLE    = BACKTEST / "data" / "val_r4_stratified.json"
OUT_DIR   = RESULTS / "phase_r1_final" / "event_charts_sym_p80" / "aggregate"

_W, _H = 1100, 600
_DARK  = "plotly_dark"

_SESSION_COLORS = {
    "regular_hours": "#26a69a",
    "pre_market":    "#FF9800",
    "post_market":   "#CE93D8",
}
_STRATUM_COLORS = {
    "low":  "#2196F3",
    "mid":  "#FF9800",
    "high": "#F44336",
}
_SESSION_LABELS = {
    "regular_hours": "Regular Hours (RTH)",
    "pre_market":    "Pre-Market",
    "post_market":   "Post-Market",
}


def _load() -> pd.DataFrame:
    trades = json.load(open(PER_TRADE))
    sample_events = {
        (e["ticker"], e["date"]): e["stratum"]
        for e in json.load(open(SAMPLE))["events"]
    }
    rows = []
    for t in trades:
        stratum = sample_events.get((t["ticker"], t["date"]), "unknown")
        rows.append({**t, "stratum": stratum})
    df = pd.DataFrame(rows)
    df["exit_ts"] = pd.to_numeric(df["exit_ts"])
    df["entry_ts"] = pd.to_numeric(df["entry_ts"])
    df["time_of_day_sec"] = pd.to_numeric(df["time_of_day_sec"])
    return df


def chart1_kde_by_session(df: pd.DataFrame, out_dir: Path) -> None:
    """KDE of PnL% for each session bucket, overlaid on one panel."""
    fig = go.Figure()

    all_pnl = df["pnl_pct"].values
    x_min, x_max = all_pnl.min() - 2, all_pnl.max() + 2
    xs = np.linspace(x_min, x_max, 600)

    for bucket in ["regular_hours", "pre_market", "post_market"]:
        sub = df[df["session_bucket"] == bucket]["pnl_pct"].values
        if len(sub) < 3:
            continue
        kde = spstats.gaussian_kde(sub, bw_method="scott")
        ys  = kde(xs)
        color = _SESSION_COLORS[bucket]
        label = _SESSION_LABELS[bucket]
        mean_v  = float(np.mean(sub))
        cvar5_v = float(np.percentile(sub, 5))
        win_pct = 100 * (sub > 0).mean()

        fig.add_trace(go.Scatter(
            x=xs, y=ys,
            mode="lines",
            fill="tozeroy",
            line=dict(color=color, width=2),
            fillcolor=_hex_to_rgba(color, 0.08),
            name=f"{label}  (n={len(sub)}, EV={mean_v:+.2f}%, WR={win_pct:.0f}%)",
            hovertemplate="PnL: %{x:.2f}%<br>Density: %{y:.4f}<extra></extra>",
        ))
        fig.add_vline(x=mean_v, line=dict(color=color, width=1.5, dash="dot"),
                      annotation_text=f"{label[:3]} mean={mean_v:+.2f}%",
                      annotation_position="top right",
                      annotation_font=dict(color=color, size=10))

    fig.add_vline(x=0, line=dict(color="#888888", width=1.5, dash="dash"))

    fig.update_layout(
        template=_DARK,
        title=dict(text="PnL% Distribution by Session — sym_p80  (KDE)", x=0.01, font=dict(size=14)),
        xaxis_title="PnL%",
        yaxis_title="Density",
        width=_W, height=_H,
        legend=dict(x=0.01, y=0.99, font=dict(size=11)),
    )
    path = out_dir / "pnl_kde_by_session.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  [1/3] pnl_kde_by_session.html  ({len(df)} trades)")


def _hex_to_rgba(hex_color: str, alpha: float) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"rgba({r},{g},{b},{alpha})"


def chart2_cum_pnl_by_tod(df: pd.DataFrame, out_dir: Path) -> None:
    """Cumulative PnL% ordered by time-of-day of entry, one line per session bucket."""
    fig = go.Figure()

    for bucket in ["pre_market", "regular_hours", "post_market"]:
        sub = df[df["session_bucket"] == bucket].sort_values("time_of_day_sec").copy()
        if sub.empty:
            continue
        sub["cum_pnl"] = sub["pnl_pct"].cumsum()

        tod_labels = [
            f"{int(s // 3600):02d}:{int((s % 3600) // 60):02d}"
            for s in sub["time_of_day_sec"]
        ]
        color = _SESSION_COLORS[bucket]
        label = _SESSION_LABELS[bucket]
        total_pnl = float(sub["pnl_pct"].sum())

        fig.add_trace(go.Scatter(
            x=tod_labels,
            y=sub["cum_pnl"].tolist(),
            mode="lines+markers",
            line=dict(color=color, width=2),
            marker=dict(size=5, color=color),
            name=f"{label}  (n={len(sub)}, total={total_pnl:+.2f}%)",
            customdata=np.column_stack([
                sub["ticker"].values,
                sub["date"].values,
                sub["pnl_pct"].values,
            ]),
            hovertemplate=(
                "%{customdata[0]} %{customdata[1]}<br>"
                "TOD: %{x}<br>"
                "PnL: %{customdata[2]:.2f}%<br>"
                "Cum: %{y:.2f}%<extra></extra>"
            ),
        ))

    fig.add_hline(y=0, line=dict(color="#555555", width=1, dash="dash"))

    fig.update_layout(
        template=_DARK,
        title=dict(text="Cumulative PnL% by Time of Day — sym_p80", x=0.01, font=dict(size=14)),
        xaxis_title="Entry time (ET, HH:MM)",
        yaxis_title="Cumulative PnL%",
        width=_W, height=_H,
        legend=dict(x=0.01, y=0.99, font=dict(size=11)),
        xaxis=dict(type="category", tickangle=45),
    )
    path = out_dir / "cum_pnl_by_tod.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  [2/3] cum_pnl_by_tod.html  ({len(df)} trades)")


def chart3_cum_pnl_by_stratum(df: pd.DataFrame, out_dir: Path) -> None:
    """Equity curve per stratum sorted by exit timestamp."""
    fig = go.Figure()

    for stratum in ["low", "mid", "high"]:
        sub = df[df["stratum"] == stratum].sort_values("exit_ts").copy()
        if sub.empty:
            continue
        sub["cum_pnl"] = sub["pnl_pct"].cumsum()
        sub["trade_num"] = np.arange(1, len(sub) + 1)

        color = _STRATUM_COLORS[stratum]
        pf_sub = sub.loc[sub["pnl_pct"] > 0, "pnl_pct"].sum() / abs(sub.loc[sub["pnl_pct"] < 0, "pnl_pct"].sum())
        wr_sub = 100 * (sub["pnl_pct"] > 0).mean()

        fig.add_trace(go.Scatter(
            x=sub["trade_num"].tolist(),
            y=sub["cum_pnl"].tolist(),
            mode="lines+markers",
            line=dict(color=color, width=2),
            marker=dict(size=5, color=color),
            name=f"{stratum.capitalize()}  (n={len(sub)}, PF={pf_sub:.3f}, WR={wr_sub:.0f}%)",
            customdata=np.column_stack([
                sub["ticker"].values,
                sub["date"].values,
                sub["session_bucket"].values,
                sub["pnl_pct"].values,
            ]),
            hovertemplate=(
                "%{customdata[0]} %{customdata[1]}<br>"
                "Session: %{customdata[2]}<br>"
                "PnL: %{customdata[3]:.2f}%<br>"
                "Cum: %{y:.2f}%  trade #%{x}<extra></extra>"
            ),
        ))

    fig.add_hline(y=0, line=dict(color="#555555", width=1, dash="dash"))

    fig.update_layout(
        template=_DARK,
        title=dict(text="Cumulative PnL% by Stratum — sym_p80  (sorted by exit time)", x=0.01, font=dict(size=14)),
        xaxis_title="Trade sequence (within stratum, sorted by exit timestamp)",
        yaxis_title="Cumulative PnL%",
        width=_W, height=_H,
        legend=dict(x=0.01, y=0.99, font=dict(size=11)),
    )
    path = out_dir / "cum_pnl_by_stratum.html"
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  [3/3] cum_pnl_by_stratum.html  ({len(df)} trades)")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Phase R1-Final sym_p80 — aggregate charts -> {OUT_DIR}")
    print()

    df = _load()
    print(f"  Loaded {len(df)} trades | strata: {df['stratum'].value_counts().to_dict()}")
    print(f"  Session buckets: {df['session_bucket'].value_counts().to_dict()}")
    print()

    chart1_kde_by_session(df, OUT_DIR)
    chart2_cum_pnl_by_tod(df, OUT_DIR)
    chart3_cum_pnl_by_stratum(df, OUT_DIR)

    print()
    print("Done.")


if __name__ == "__main__":
    main()
