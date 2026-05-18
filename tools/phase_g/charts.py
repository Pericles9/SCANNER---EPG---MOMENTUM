"""Phase G -- T6: 12 standalone Plotly HTML diagnostic charts.

Reads: results/phase_g/*.parquet (produced by run_analysis.py T1-T5)
Writes: results/phase_g/charts/01_*.html ... 12_*.html

Usage:
    python -m tools.phase_g.charts
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT))

_G_DIR = _ROOT / "results" / "phase_g"
_CHARTS_DIR = _G_DIR / "charts"

_W = 1200
_H = 600

_EXIT_COLORS = {
    "luld_upper": "#FF6600",
    "exit_d": "#3366FF",
    "epg_window_close": "#00AA44",
}
_SESSION_COLORS = {
    "pre_market": "#9966CC",
    "rth": "#3366FF",
    "post_market": "#FF6600",
}
_RANK_PALETTE = [
    "#FF3300", "#FF6600", "#FF9900", "#FFCC00", "#99CC00",
    "#33AA44", "#0099CC", "#3366FF", "#6633CC", "#999999",
]


def _write(fig: go.Figure, name: str) -> None:
    path = _CHARTS_DIR / name
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  {name}")


def _pf_color(pf: float) -> str:
    return "#00AA44" if (not np.isnan(pf) and pf >= 1.0) else "#CC2200"


# ---------------------------------------------------------------------------
# Chart 1: PnL% vs pct_change scatter (scanner_context)
# ---------------------------------------------------------------------------

def chart01_pnl_vs_pct_change(ctx: pd.DataFrame) -> None:
    valid = ctx[ctx["traded_pct_change"].notna() & ctx["scanner_rank"].notna()].copy()
    valid["scanner_rank"] = valid["scanner_rank"].astype(int)

    fig = go.Figure()

    top_ranks = sorted(valid["scanner_rank"].unique())[:10]
    for rank in top_ranks:
        sub = valid[valid["scanner_rank"] == rank]
        color = _RANK_PALETTE[min(rank - 1, len(_RANK_PALETTE) - 1)]
        fig.add_trace(go.Scatter(
            x=sub["traded_pct_change"] * 100,
            y=sub["pnl_pct"],
            mode="markers",
            name=f"Rank {rank}",
            marker=dict(color=color, size=5, opacity=0.6),
            hovertemplate=(
                f"Rank {rank}<br>"
                "pct_change=%{x:.2f}%<br>"
                "pnl=%{y:.3f}%<extra></extra>"
            ),
        ))

    rest = valid[valid["scanner_rank"] > 10]
    if len(rest) > 0:
        fig.add_trace(go.Scatter(
            x=rest["traded_pct_change"] * 100,
            y=rest["pnl_pct"],
            mode="markers",
            name="Rank 11+",
            marker=dict(color="#CCCCCC", size=4, opacity=0.4),
            hovertemplate="Rank 11+<br>pct_change=%{x:.2f}%<br>pnl=%{y:.3f}%<extra></extra>",
        ))

    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1))
    fig.add_vline(x=0, line=dict(color="#888888", dash="dot", width=1))

    fig.update_layout(
        title="Phase G -- PnL% vs Scanner Momentum (% Change from Prev Close) by Rank",
        xaxis_title="Scanner % Change from Prev Close (%)",
        yaxis_title="Trade PnL%",
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "01_pnl_vs_pct_change_scatter.html")


# ---------------------------------------------------------------------------
# Chart 2: EV by scanner rank (with CI error bars)
# ---------------------------------------------------------------------------

def chart02_ev_by_rank(rank_stats: pd.DataFrame) -> None:
    df = rank_stats.sort_values("scanner_rank")
    colors = ["#00AA44" if ev >= 0 else "#CC2200" for ev in df["ev"]]
    flags = df["low_n_flag"].fillna(False)

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["scanner_rank"],
        y=df["ev"],
        marker_color=colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["ev_ci_high"] - df["ev"]).clip(lower=0).tolist(),
            arrayminus=(df["ev"] - df["ev_ci_low"]).clip(lower=0).tolist(),
            thickness=1.5,
            width=4,
        ),
        customdata=np.column_stack([
            df["n_trades"].values,
            flags.values.astype(int),
        ]),
        hovertemplate=(
            "Rank %{x}<br>"
            "EV=%{y:.4f}%<br>"
            "n=%{customdata[0]}<br>"
            "low_n=%{customdata[1]}<extra></extra>"
        ),
    ))
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1))

    fig.update_layout(
        title="Phase G -- Expected Value (mean PnL%) by Scanner Rank (95% bootstrap CI)",
        xaxis_title="Scanner Rank at Entry",
        yaxis_title="Mean PnL%",
        xaxis=dict(dtick=1),
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "02_ev_by_rank.html")


# ---------------------------------------------------------------------------
# Chart 3: PF by scanner rank (with CI error bars)
# ---------------------------------------------------------------------------

def chart03_pf_by_rank(rank_stats: pd.DataFrame) -> None:
    df = rank_stats.sort_values("scanner_rank")
    colors = [_pf_color(pf) for pf in df["pf"]]

    fig = go.Figure()
    fig.add_trace(go.Bar(
        x=df["scanner_rank"],
        y=df["pf"].fillna(0),
        marker_color=colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["pf_ci_high"] - df["pf"]).clip(lower=0).fillna(0).tolist(),
            arrayminus=(df["pf"] - df["pf_ci_low"]).clip(lower=0).fillna(0).tolist(),
            thickness=1.5,
            width=4,
        ),
        customdata=df["n_trades"].values,
        hovertemplate=(
            "Rank %{x}<br>"
            "PF=%{y:.4f}<br>"
            "n=%{customdata}<extra></extra>"
        ),
    ))
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1),
                  annotation_text="PF=1.0", annotation_position="bottom right")
    fig.add_hline(y=1.9194, line=dict(color="#3366FF", dash="dot", width=1.5),
                  annotation_text="Phase F val-full PF=1.9194",
                  annotation_position="top right")

    fig.update_layout(
        title="Phase G -- Profit Factor by Scanner Rank (95% bootstrap CI)",
        xaxis_title="Scanner Rank at Entry",
        yaxis_title="Profit Factor",
        xaxis=dict(dtick=1),
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "03_pf_by_rank.html")


# ---------------------------------------------------------------------------
# Chart 4: EV and PF by scanner heat bin
# ---------------------------------------------------------------------------

def chart04_ev_pf_by_heat_bin(heat_bin_stats: pd.DataFrame) -> None:
    df = heat_bin_stats.copy()
    bin_order = ["cold_Q1", "Q2", "Q3", "hot_Q4"]
    df["heat_bin"] = pd.Categorical(df["heat_bin"], categories=bin_order, ordered=True)
    df = df.sort_values("heat_bin")

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=["Mean PnL% by Heat Bin (95% CI)", "Profit Factor by Heat Bin (95% CI)"],
    )

    ev_colors = ["#00AA44" if ev >= 0 else "#CC2200" for ev in df["ev"]]
    fig.add_trace(go.Bar(
        x=df["heat_bin"],
        y=df["ev"],
        marker_color=ev_colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["ev_ci_high"] - df["ev"]).clip(lower=0).tolist(),
            arrayminus=(df["ev"] - df["ev_ci_low"]).clip(lower=0).tolist(),
            thickness=1.5,
            width=6,
        ),
        customdata=df["n_trades"].values,
        hovertemplate="Heat=%{x}<br>EV=%{y:.4f}%<br>n=%{customdata}<extra></extra>",
        name="EV",
    ), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=1)

    pf_colors = [_pf_color(pf) for pf in df["pf"]]
    fig.add_trace(go.Bar(
        x=df["heat_bin"],
        y=df["pf"].fillna(0),
        marker_color=pf_colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["pf_ci_high"] - df["pf"]).clip(lower=0).fillna(0).tolist(),
            arrayminus=(df["pf"] - df["pf_ci_low"]).clip(lower=0).fillna(0).tolist(),
            thickness=1.5,
            width=6,
        ),
        customdata=df["n_trades"].values,
        hovertemplate="Heat=%{x}<br>PF=%{y:.4f}<br>n=%{customdata}<extra></extra>",
        name="PF",
        showlegend=False,
    ), row=1, col=2)
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=2)

    fig.update_layout(
        title="Phase G -- EV and PF by Scanner Heat (75th Percentile % Change) Quartile",
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "04_ev_pf_by_heat_bin.html")


# ---------------------------------------------------------------------------
# Chart 5: Rank x Heat interaction heatmap (EV)
# ---------------------------------------------------------------------------

def chart05_rank_heat_interaction(rh: pd.DataFrame) -> None:
    bins = ["cold_Q1", "hot_Q4"]
    ranks = sorted(rh["scanner_rank"].unique())

    ev_matrix = []
    for heat in bins:
        row = []
        for rank in ranks:
            sub = rh[(rh["scanner_rank"] == rank) & (rh["heat_bin"] == heat)]
            ev = float(sub["ev"].iloc[0]) if len(sub) > 0 else float("nan")
            row.append(ev)
        ev_matrix.append(row)

    n_matrix = []
    for heat in bins:
        row = []
        for rank in ranks:
            sub = rh[(rh["scanner_rank"] == rank) & (rh["heat_bin"] == heat)]
            n = int(sub["n_trades"].iloc[0]) if len(sub) > 0 else 0
            row.append(n)
        n_matrix.append(row)

    text_matrix = []
    for i, heat in enumerate(bins):
        row = []
        for j, rank in enumerate(ranks):
            ev_v = ev_matrix[i][j]
            n_v = n_matrix[i][j]
            txt = f"{ev_v:.3f}%\nn={n_v}" if not np.isnan(ev_v) else "n/a"
            row.append(txt)
        text_matrix.append(row)

    fig = go.Figure(go.Heatmap(
        z=ev_matrix,
        x=[str(r) for r in ranks],
        y=bins,
        text=text_matrix,
        texttemplate="%{text}",
        colorscale="RdYlGn",
        zmid=0,
        colorbar=dict(title="Mean PnL%"),
        hovertemplate="Rank %{x}<br>Heat: %{y}<br>EV=%{z:.4f}%<extra></extra>",
    ))
    fig.update_layout(
        title="Phase G -- EV Interaction: Scanner Rank x Heat (cold Q1 vs hot Q4), Ranks 1-10",
        xaxis_title="Scanner Rank",
        yaxis_title="Heat Bin",
        width=_W, height=max(400, _H // 2),
        template="plotly_white",
    )
    _write(fig, "05_rank_heat_interaction.html")


# ---------------------------------------------------------------------------
# Chart 6: TOD EV and PF (dual axis)
# ---------------------------------------------------------------------------

def chart06_tod_ev_pf(tod: pd.DataFrame) -> None:
    df = tod.sort_values("bucket_sec")

    session_colors = [_SESSION_COLORS.get(s, "#888888") for s in df["session"]]

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    fig.add_trace(go.Bar(
        x=df["bucket_label"],
        y=df["ev"],
        marker_color=session_colors,
        opacity=0.75,
        customdata=np.column_stack([df["n_trades"].values, df["session"].values]),
        hovertemplate="Time=%{x}<br>EV=%{y:.4f}%<br>n=%{customdata[0]}<br>session=%{customdata[1]}<extra></extra>",
        name="EV (mean PnL%)",
    ), secondary_y=False)

    fig.add_trace(go.Scatter(
        x=df["bucket_label"],
        y=df["pf"].replace(float("nan"), None),
        mode="lines+markers",
        line=dict(color="#FF6600", width=2),
        marker=dict(size=6),
        name="Profit Factor",
        hovertemplate="Time=%{x}<br>PF=%{y:.4f}<extra></extra>",
    ), secondary_y=True)

    fig.add_hline(y=0, line=dict(color="#AAAAAA", dash="dash", width=1), secondary_y=False)
    fig.add_hline(y=1.0, line=dict(color="#FF6600", dash="dot", width=1), secondary_y=True)

    tick_step = max(1, len(df) // 20)
    fig.update_xaxes(tickangle=45, tickmode="array",
                     tickvals=df["bucket_label"].iloc[::tick_step].tolist())

    fig.update_yaxes(title_text="Mean PnL%", secondary_y=False)
    fig.update_yaxes(title_text="Profit Factor", secondary_y=True)

    fig.update_layout(
        title="Phase G -- Time-of-Day: EV (bars) and PF (line) by 10-min Bucket",
        xaxis_title="Time (ET, 4:00 AM base)",
        width=_W, height=_H,
        template="plotly_white",
        legend=dict(x=0.01, y=0.99),
    )
    _write(fig, "06_tod_ev_pf.html")


# ---------------------------------------------------------------------------
# Chart 7: TOD win rate and mean hold time
# ---------------------------------------------------------------------------

def chart07_tod_wr_hold(tod: pd.DataFrame) -> None:
    df = tod.sort_values("bucket_sec")

    fig = make_subplots(specs=[[{"secondary_y": True}]])

    session_colors = [_SESSION_COLORS.get(s, "#888888") for s in df["session"]]

    fig.add_trace(go.Scatter(
        x=df["bucket_label"],
        y=df["win_rate"] * 100,
        mode="lines+markers",
        line=dict(color="#3366FF", width=2),
        marker=dict(color=session_colors, size=7, line=dict(width=1, color="#222222")),
        name="Win Rate %",
        hovertemplate="Time=%{x}<br>WR=%{y:.1f}%<extra></extra>",
    ), secondary_y=False)

    fig.add_trace(go.Bar(
        x=df["bucket_label"],
        y=df["mean_hold_sec"],
        opacity=0.4,
        marker_color="#AAAAAA",
        name="Mean Hold (s)",
        hovertemplate="Time=%{x}<br>Hold=%{y:.0f}s<extra></extra>",
    ), secondary_y=True)

    fig.add_hline(y=50.0, line=dict(color="#AAAAAA", dash="dash", width=1),
                  annotation_text="50% WR", annotation_position="right", secondary_y=False)

    tick_step = max(1, len(df) // 20)
    fig.update_xaxes(tickangle=45, tickmode="array",
                     tickvals=df["bucket_label"].iloc[::tick_step].tolist())

    fig.update_yaxes(title_text="Win Rate %", secondary_y=False)
    fig.update_yaxes(title_text="Mean Hold (seconds)", secondary_y=True)

    fig.update_layout(
        title="Phase G -- Time-of-Day: Win Rate (line) and Mean Hold Time (bars)",
        xaxis_title="Time (ET, 4:00 AM base)",
        width=_W, height=_H,
        template="plotly_white",
        legend=dict(x=0.01, y=0.99),
    )
    _write(fig, "07_tod_wr_hold.html")


# ---------------------------------------------------------------------------
# Chart 8: Exit type share by scanner rank (stacked bar)
# ---------------------------------------------------------------------------

def chart08_exit_by_rank(exit_ctx: pd.DataFrame) -> None:
    df = exit_ctx[exit_ctx["dimension"] == "rank"].copy()
    df["key_int"] = df["key"].astype(int)
    df = df.sort_values("key_int")

    exit_cols = ["luld_upper_share", "exit_d_share", "epg_window_close_share"]
    exit_labels = ["luld_upper", "exit_d", "epg_window_close"]

    fig = go.Figure()
    for col, label in zip(exit_cols, exit_labels):
        color = _EXIT_COLORS.get(label, "#888888")
        fig.add_trace(go.Bar(
            x=df["key"],
            y=df[col] * 100,
            name=label,
            marker_color=color,
            customdata=df["n_trades"].values,
            hovertemplate=f"{label}<br>Rank=%{{x}}<br>Share=%{{y:.1f}}%<br>n=%{{customdata}}<extra></extra>",
        ))

    fig.update_layout(
        title="Phase G -- Exit Type Share by Scanner Rank (stacked %)",
        xaxis_title="Scanner Rank at Entry",
        yaxis_title="Exit Type Share (%)",
        barmode="stack",
        width=_W, height=_H,
        template="plotly_white",
        xaxis=dict(type="category"),
    )
    _write(fig, "08_exit_type_by_rank.html")


# ---------------------------------------------------------------------------
# Chart 9: Exit type share by heat bin (stacked bar)
# ---------------------------------------------------------------------------

def chart09_exit_by_heat(exit_ctx: pd.DataFrame) -> None:
    df = exit_ctx[exit_ctx["dimension"] == "heat_bin"].copy()
    bin_order = ["cold_Q1", "Q2", "Q3", "hot_Q4"]
    df["heat_bin"] = pd.Categorical(df["key"], categories=bin_order, ordered=True)
    df = df.sort_values("heat_bin")

    exit_cols = ["luld_upper_share", "exit_d_share", "epg_window_close_share"]
    exit_labels = ["luld_upper", "exit_d", "epg_window_close"]

    fig = go.Figure()
    for col, label in zip(exit_cols, exit_labels):
        color = _EXIT_COLORS.get(label, "#888888")
        fig.add_trace(go.Bar(
            x=df["key"],
            y=df[col] * 100,
            name=label,
            marker_color=color,
            customdata=df["n_trades"].values,
            hovertemplate=f"{label}<br>Heat=%{{x}}<br>Share=%{{y:.1f}}%<br>n=%{{customdata}}<extra></extra>",
        ))

    fig.update_layout(
        title="Phase G -- Exit Type Share by Scanner Heat Bin (stacked %)",
        xaxis_title="Scanner Heat Bin",
        yaxis_title="Exit Type Share (%)",
        barmode="stack",
        width=_W, height=_H,
        template="plotly_white",
        xaxis=dict(type="category", categoryorder="array", categoryarray=bin_order),
    )
    _write(fig, "09_exit_type_by_heat.html")


# ---------------------------------------------------------------------------
# Chart 10: EV and PF by scanner size bin
# ---------------------------------------------------------------------------

def chart10_ev_pf_by_scanner_size(size_stats: pd.DataFrame) -> None:
    df = size_stats.sort_values("mean_scanner_n")

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=["Mean PnL% by Scanner Size (95% CI)", "Profit Factor by Scanner Size (95% CI)"],
    )

    ev_colors = ["#00AA44" if ev >= 0 else "#CC2200" for ev in df["ev"]]
    fig.add_trace(go.Bar(
        x=df["size_bin"],
        y=df["ev"],
        marker_color=ev_colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["ev_ci_high"] - df["ev"]).clip(lower=0).tolist(),
            arrayminus=(df["ev"] - df["ev_ci_low"]).clip(lower=0).tolist(),
            thickness=1.5,
            width=6,
        ),
        customdata=np.column_stack([df["n_trades"].values, df["mean_scanner_n"].values]),
        hovertemplate="Bin=%{x}<br>EV=%{y:.4f}%<br>n=%{customdata[0]}<br>mean_scanner_n=%{customdata[1]:.1f}<extra></extra>",
        name="EV",
    ), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=1)

    pf_colors = [_pf_color(pf) for pf in df["pf"]]
    fig.add_trace(go.Bar(
        x=df["size_bin"],
        y=df["pf"].fillna(0),
        marker_color=pf_colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["pf_ci_high"] - df["pf"]).clip(lower=0).fillna(0).tolist(),
            arrayminus=(df["pf"] - df["pf_ci_low"]).clip(lower=0).fillna(0).tolist(),
            thickness=1.5,
            width=6,
        ),
        customdata=df["n_trades"].values,
        hovertemplate="Bin=%{x}<br>PF=%{y:.4f}<br>n=%{customdata}<extra></extra>",
        name="PF",
        showlegend=False,
    ), row=1, col=2)
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=2)

    fig.update_layout(
        title="Phase G -- EV and PF by Scanner Size (n active tickers) Quartile",
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "10_ev_pf_by_scanner_size.html")


# ---------------------------------------------------------------------------
# Chart 11: Multi-day runner comparison
# ---------------------------------------------------------------------------

def chart11_multi_day_runner(runner_stats: pd.DataFrame) -> None:
    df = runner_stats.copy()
    groups = df["group"].tolist()
    colors = ["#FF6600" if g == "multi_day_runner" else "#3366FF" for g in groups]

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=["Mean PnL% (95% CI)", "Profit Factor (95% CI)", "Win Rate"],
    )

    fig.add_trace(go.Bar(
        x=groups, y=df["ev"],
        marker_color=colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["ev_ci_high"] - df["ev"]).clip(lower=0).tolist(),
            arrayminus=(df["ev"] - df["ev_ci_low"]).clip(lower=0).tolist(),
            thickness=1.5,
            width=8,
        ),
        customdata=df["n_trades"].values,
        hovertemplate="%{x}<br>EV=%{y:.4f}%<br>n=%{customdata}<extra></extra>",
        name="EV",
    ), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=1)

    fig.add_trace(go.Bar(
        x=groups, y=df["pf"].fillna(0),
        marker_color=colors,
        error_y=dict(
            type="data",
            symmetric=False,
            array=(df["pf_ci_high"] - df["pf"]).clip(lower=0).fillna(0).tolist(),
            arrayminus=(df["pf"] - df["pf_ci_low"]).clip(lower=0).fillna(0).tolist(),
            thickness=1.5,
            width=8,
        ),
        customdata=df["n_trades"].values,
        hovertemplate="%{x}<br>PF=%{y:.4f}<br>n=%{customdata}<extra></extra>",
        name="PF",
        showlegend=False,
    ), row=1, col=2)
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=2)

    fig.add_trace(go.Bar(
        x=groups, y=df["win_rate"] * 100,
        marker_color=colors,
        customdata=df["n_trades"].values,
        hovertemplate="%{x}<br>WR=%{y:.1f}%<br>n=%{customdata}<extra></extra>",
        name="WR",
        showlegend=False,
    ), row=1, col=3)
    fig.add_hline(y=50.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=3)

    fig.update_layout(
        title="Phase G -- Multi-Day Runner vs Fresh Event: EV, PF, Win Rate",
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "11_multi_day_runner.html")


# ---------------------------------------------------------------------------
# Chart 12: Entry lag (time_of_day_sec proxy)
# ---------------------------------------------------------------------------

def chart12_entry_lag(lag_stats: pd.DataFrame) -> None:
    df = lag_stats.copy()

    fig = make_subplots(
        rows=1, cols=3,
        subplot_titles=["Mean PnL% by Session Age Bucket", "Profit Factor", "Win Rate %"],
    )

    ev_colors = ["#00AA44" if ev >= 0 else "#CC2200" for ev in df["ev"]]
    fig.add_trace(go.Bar(
        x=df["lag_bucket"],
        y=df["ev"],
        marker_color=ev_colors,
        customdata=df["n_trades"].values,
        hovertemplate="Bucket=%{x}<br>EV=%{y:.4f}%<br>n=%{customdata}<extra></extra>",
        name="EV",
    ), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=1)

    pf_colors = [_pf_color(pf) for pf in df["pf"]]
    fig.add_trace(go.Bar(
        x=df["lag_bucket"],
        y=df["pf"].fillna(0),
        marker_color=pf_colors,
        customdata=df["n_trades"].values,
        hovertemplate="Bucket=%{x}<br>PF=%{y:.4f}<br>n=%{customdata}<extra></extra>",
        name="PF",
        showlegend=False,
    ), row=1, col=2)
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=2)

    fig.add_trace(go.Bar(
        x=df["lag_bucket"],
        y=df["win_rate"] * 100,
        marker_color="#3366FF",
        customdata=df["n_trades"].values,
        hovertemplate="Bucket=%{x}<br>WR=%{y:.1f}%<br>n=%{customdata}<extra></extra>",
        name="WR",
        showlegend=False,
    ), row=1, col=3)
    fig.add_hline(y=50.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=3)

    fig.update_layout(
        title=(
            "Phase G -- Entry Lag (Session Age Proxy): EV, PF, Win Rate by Time-of-Day Bucket\n"
            "Note: no t_event in per_trade; time_of_day_sec used as session-age proxy"
        ),
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "12_entry_lag.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    def _load(name: str) -> pd.DataFrame:
        path = _G_DIR / name
        if not path.exists():
            print(f"ERROR: {path} not found -- run run_analysis.py first")
            raise SystemExit(1)
        return pd.read_parquet(path)

    ctx = _load("scanner_context.parquet")
    tod = _load("tod_stats.parquet")
    rank_stats = _load("rank_stats.parquet")
    heat_bin_stats = _load("heat_bin_stats.parquet")
    rh_interaction = _load("rank_heat_interaction.parquet")
    exit_ctx = _load("exit_by_scanner_context.parquet")
    size_stats = _load("scanner_size_stats.parquet")
    runner_stats = _load("multi_day_runner_stats.parquet")
    lag_stats = _load("entry_lag_stats.parquet")

    print(f"Phase G charts -> {_CHARTS_DIR}/")
    print()

    chart01_pnl_vs_pct_change(ctx)
    chart02_ev_by_rank(rank_stats)
    chart03_pf_by_rank(rank_stats)
    chart04_ev_pf_by_heat_bin(heat_bin_stats)
    chart05_rank_heat_interaction(rh_interaction)
    chart06_tod_ev_pf(tod)
    chart07_tod_wr_hold(tod)
    chart08_exit_by_rank(exit_ctx)
    chart09_exit_by_heat(exit_ctx)
    chart10_ev_pf_by_scanner_size(size_stats)
    chart11_multi_day_runner(runner_stats)
    chart12_entry_lag(lag_stats)

    print(f"\nAll 12 charts written to {_CHARTS_DIR}/")


if __name__ == "__main__":
    main()
