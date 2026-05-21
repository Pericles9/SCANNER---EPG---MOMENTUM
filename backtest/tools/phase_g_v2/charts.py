"""Phase G v2 -- T3: 4 standalone Plotly HTML diagnostic charts.

Reads: results/phase_g_v2/*.parquet
Writes: results/phase_g_v2/charts/01_*.html ... 04_*.html

Usage:
    python -m tools.phase_g_v2.charts
"""
from __future__ import annotations

import pathlib
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_ROOT = pathlib.Path(__file__).resolve().parents[2]
_V2_DIR = _ROOT / "results" / "phase_g_v2"
_CHARTS_DIR = _V2_DIR / "charts"

_W = 1200
_H = 600
PHASE_F_PF = 1.9194

_Q_COLORS = {1: "#3366FF", 2: "#00AA44", 3: "#FF9900", 4: "#CC2200"}
_Q_LABELS = {1: "Q1 (top momentum)", 2: "Q2", 3: "Q3", 4: "Q4 (bottom momentum)"}

_EXIT_COLORS = {
    "luld_upper": "#FF6600",
    "exit_d": "#3366FF",
    "epg_window_close": "#00AA44",
}


def _write(fig: go.Figure, name: str) -> None:
    path = _CHARTS_DIR / name
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  {name}")


def _pf_color(pf: float) -> str:
    if np.isnan(pf):
        return "#AAAAAA"
    return "#00AA44" if pf >= 1.0 else "#CC2200"


# ---------------------------------------------------------------------------
# T3a: EV and PF by scanner_quartile
# ---------------------------------------------------------------------------

def chart01_ev_pf_by_quartile(qs: pd.DataFrame) -> None:
    df = qs.sort_values("scanner_quartile")
    labels = [_Q_LABELS.get(int(q), f"Q{q}") for q in df["scanner_quartile"]]
    colors = [_Q_COLORS.get(int(q), "#888888") for q in df["scanner_quartile"]]

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=[
            "Mean PnL% by Momentum-Weighted Quartile (95% CI)",
            "Profit Factor by Momentum-Weighted Quartile (95% CI)",
        ],
    )

    ev_colors = ["#00AA44" if ev >= 0 else "#CC2200" for ev in df["ev"]]
    fig.add_trace(go.Bar(
        x=labels,
        y=df["ev"],
        marker_color=ev_colors,
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
        text=df["n_trades"].apply(lambda n: f"n={n}"),
        textposition="outside",
        name="EV",
    ), row=1, col=1)
    fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=1)

    pf_colors = [_pf_color(pf) for pf in df["pf"]]
    fig.add_trace(go.Bar(
        x=labels,
        y=df["pf"].fillna(0),
        marker_color=pf_colors,
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
        text=df["pf"].apply(lambda p: f"{p:.3f}" if not np.isnan(p) else "—"),
        textposition="outside",
        name="PF",
        showlegend=False,
    ), row=1, col=2)
    fig.add_hline(y=1.0, line=dict(color="#888888", dash="dash", width=1), row=1, col=2)
    fig.add_hline(
        y=PHASE_F_PF,
        line=dict(color="#3366FF", dash="dot", width=1.5),
        annotation_text=f"Phase F baseline PF={PHASE_F_PF}",
        annotation_position="top right",
        row=1, col=2,
    )

    fig.update_layout(
        title=(
            "Phase G v2 — EV and PF by Momentum-Weighted Scanner Quartile\n"
            "Q1 = top momentum (largest share of snapshot momentum), Q4 = bottom"
        ),
        width=_W, height=_H,
        template="plotly_white",
    )
    _write(fig, "01_ev_pf_by_quartile.html")


# ---------------------------------------------------------------------------
# T3b: Rank x Quartile interaction heatmap (EV)
# ---------------------------------------------------------------------------

def chart02_rank_quartile_interaction(rq: pd.DataFrame) -> None:
    ranks = sorted(rq["scanner_rank"].unique())
    quartiles = [1, 4]
    q_labels = {1: "Q1 (top momentum)", 4: "Q4 (bottom momentum)"}

    ev_matrix = []
    n_matrix = []
    text_matrix = []
    low_n_matrix = []

    for q in quartiles:
        ev_row, n_row, text_row, low_n_row = [], [], [], []
        for rank in ranks:
            sub = rq[(rq["scanner_rank"] == rank) & (rq["scanner_quartile"] == q)]
            ev = float(sub["ev"].iloc[0]) if len(sub) > 0 else float("nan")
            n = int(sub["n_trades"].iloc[0]) if len(sub) > 0 else 0
            low_n = bool(sub["low_n_flag"].iloc[0]) if len(sub) > 0 else True
            ev_row.append(ev)
            n_row.append(n)
            low_n_row.append(low_n)
            txt = f"{ev:.3f}%\nn={n}" + (" *" if low_n else "")
            text_row.append(txt)
        ev_matrix.append(ev_row)
        n_matrix.append(n_row)
        text_matrix.append(text_row)
        low_n_matrix.append(low_n_row)

    fig = go.Figure(go.Heatmap(
        z=ev_matrix,
        x=[str(r) for r in ranks],
        y=[q_labels[q] for q in quartiles],
        text=text_matrix,
        texttemplate="%{text}",
        colorscale="RdYlGn",
        zmid=0,
        colorbar=dict(title="Mean PnL%"),
        hovertemplate="Rank %{x}<br>%{y}<br>EV=%{z:.4f}%<extra></extra>",
    ))
    fig.update_layout(
        title=(
            "Phase G v2 — EV by Scanner Rank (1–10) × Momentum Quartile (Q1 vs Q4)\n"
            "* = n < 20 (wide CI, interpret with caution)"
        ),
        xaxis_title="Scanner Rank at Entry",
        yaxis_title="Momentum-Weighted Quartile",
        width=_W, height=500,
        template="plotly_white",
    )
    _write(fig, "02_rank_quartile_interaction.html")


# ---------------------------------------------------------------------------
# T3c: Exit type share by scanner_quartile (stacked bar)
# ---------------------------------------------------------------------------

def chart03_exit_by_quartile(exit_q: pd.DataFrame) -> None:
    df = exit_q.sort_values("scanner_quartile")
    x_labels = [_Q_LABELS.get(int(q), f"Q{q}") for q in df["scanner_quartile"]]

    exit_cols = ["luld_upper_share", "exit_d_share", "epg_window_close_share"]
    exit_labels = ["luld_upper", "exit_d", "epg_window_close"]

    fig = go.Figure()
    for col, label in zip(exit_cols, exit_labels):
        color = _EXIT_COLORS.get(label, "#888888")
        flags = df["flag_luld_elevated"].values if col == "luld_upper_share" else [False] * len(df)
        fig.add_trace(go.Bar(
            x=x_labels,
            y=df[col] * 100,
            name=label,
            marker_color=color,
            customdata=np.column_stack([
                df["n_trades"].values,
                df["luld_upper_vs_pop"].values,
            ]),
            hovertemplate=(
                f"{label}<br>Quartile=%{{x}}<br>"
                "Share=%{y:.1f}%<br>n=%{customdata[0]}<br>"
                "luld_vs_pop=%{customdata[1]:.2f}<extra></extra>"
            ),
        ))

    fig.update_layout(
        title="Phase G v2 — Exit Type Share by Momentum-Weighted Scanner Quartile (stacked %)",
        xaxis_title="Scanner Momentum Quartile",
        yaxis_title="Exit Type Share (%)",
        barmode="stack",
        width=_W, height=_H,
        template="plotly_white",
        xaxis=dict(type="category"),
    )
    _write(fig, "03_exit_by_quartile.html")


# ---------------------------------------------------------------------------
# T3d: scanner_n=1 isolation
# ---------------------------------------------------------------------------

def chart04_scanner_n1_isolation(n1: pd.DataFrame) -> None:
    groups = ["scanner_n=1", "scanner_n>1"]
    subsets = ["all_ranks", "rank_1_only"]
    subset_labels = {"all_ranks": "All Ranks", "rank_1_only": "Rank 1 Only"}
    group_colors = {"scanner_n=1": "#FF6600", "scanner_n>1": "#3366FF"}

    fig = make_subplots(
        rows=1, cols=2,
        subplot_titles=[
            "All Ranks: EV and PF by Scanner Size",
            "Rank 1 Only: EV and PF by Scanner Size",
        ],
        specs=[[{"secondary_y": False}, {"secondary_y": False}]],
    )

    for col_idx, subset in enumerate(subsets, 1):
        sub_df = n1[n1["subset"] == subset]

        for group in groups:
            row = sub_df[sub_df["group"] == group]
            if len(row) == 0:
                continue
            ev = float(row["ev"].iloc[0])
            pf = float(row["pf"].iloc[0]) if not np.isnan(float(row["pf"].iloc[0])) else 0.0
            n = int(row["n_trades"].iloc[0])
            color = group_colors[group]

            fig.add_trace(go.Bar(
                x=[f"{group} EV", f"{group} PF"],
                y=[ev, pf],
                name=group,
                marker_color=color,
                showlegend=(col_idx == 1),
                customdata=[[n, n]],
                hovertemplate=f"{group}<br>%{{x}}=%{{y:.4f}}<br>n={n}<extra></extra>",
                text=[f"{ev:+.3f}%", f"PF={pf:.3f}"],
                textposition="outside",
            ), row=1, col=col_idx)

        fig.add_hline(y=0, line=dict(color="#888888", dash="dash", width=1), row=1, col=col_idx)
        fig.add_hline(y=1.0, line=dict(color="#AAAAAA", dash="dot", width=1), row=1, col=col_idx)

    fig.update_layout(
        title=(
            "Phase G v2 — scanner_n=1 Isolation: Does Single-Name Scanner Drive Rank 1 Underperformance?\n"
            "Left: all ranks | Right: rank 1 only | Orange = single-name days | Blue = multi-name days"
        ),
        width=_W, height=_H,
        template="plotly_white",
        barmode="group",
    )
    _write(fig, "04_scanner_n1_isolation.html")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    _CHARTS_DIR.mkdir(parents=True, exist_ok=True)

    def _load(name: str) -> pd.DataFrame:
        path = _V2_DIR / name
        if not path.exists():
            print(f"ERROR: {path} not found -- run run_v2.py first")
            raise SystemExit(1)
        return pd.read_parquet(path)

    qs = _load("quartile_stats.parquet")
    rq = _load("rank_quartile_interaction.parquet")
    exit_q = _load("exit_by_quartile.parquet")
    n1 = _load("scanner_n1_isolation.parquet")

    print(f"Phase G v2 charts -> {_CHARTS_DIR}/")
    print()

    chart01_ev_pf_by_quartile(qs)
    chart02_rank_quartile_interaction(rq)
    chart03_exit_by_quartile(exit_q)
    chart04_scanner_n1_isolation(n1)

    print(f"\nAll 4 charts written to {_CHARTS_DIR}/")


if __name__ == "__main__":
    main()
