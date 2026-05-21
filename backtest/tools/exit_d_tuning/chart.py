"""Single-chart builder for one (theta, tau_min) combination."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from tools.exit_d_tuning.replay import EventReplay
from tools.exit_d_tuning.simulate import ExitDSimulation


@dataclass
class ChartSummary:
    theta: float
    tau_min_sec: float
    n_trades: int
    n_exit_d_fired: int
    n_disabled_high_i: int
    sum_original_pnl_pct: float
    sum_exit_d_pnl_pct: float       # NaN-safe sum: original_pnl when EXIT_D didn't fire
    sum_delta_pnl_pct: float        # sum_exit_d_pnl_pct - sum_original_pnl_pct
    n_improved_trades: int
    n_worsened_trades: int
    chart_filename: str             # relative path from index.html


def _summarize(sim: ExitDSimulation, theta: float, tau_min_sec: float,
               chart_filename: str) -> ChartSummary:
    n_trades = len(sim.trade_seq)
    n_fired = int(sim.exit_d_would_fire.sum())
    n_disabled = int(np.sum(sim.exit_d_reason == "disabled_high_i_at_entry"))

    sum_orig = float(sim.original_pnl_pct.sum()) if n_trades else 0.0

    # NaN-safe sum: when EXIT_D didn't fire, the trade's contribution is the
    # original pnl (the trade still happened). When EXIT_D did fire, the
    # contribution is the EXIT_D pnl.
    composite = np.where(sim.exit_d_would_fire,
                          sim.exit_d_pnl_pct, sim.original_pnl_pct)
    sum_ed = float(np.nansum(composite)) if n_trades else 0.0

    # Per-trade delta = exit_d_pnl - original_pnl (only meaningful when fired)
    delta = np.where(sim.exit_d_would_fire,
                      sim.exit_d_pnl_pct - sim.original_pnl_pct, 0.0)
    sum_delta = float(np.nansum(delta)) if n_trades else 0.0
    n_improved = int(np.sum(delta > 0))
    n_worsened = int(np.sum(delta < 0))

    return ChartSummary(
        theta=float(theta),
        tau_min_sec=float(tau_min_sec),
        n_trades=int(n_trades),
        n_exit_d_fired=n_fired,
        n_disabled_high_i=n_disabled,
        sum_original_pnl_pct=sum_orig,
        sum_exit_d_pnl_pct=sum_ed,
        sum_delta_pnl_pct=sum_delta,
        n_improved_trades=n_improved,
        n_worsened_trades=n_worsened,
        chart_filename=chart_filename,
    )


def make_exit_d_chart(
    replay: EventReplay,
    phase_s_trades: pd.DataFrame,
    sim: ExitDSimulation,
    theta: float,
    tau_min_sec: float,
    output_path: Path,
) -> ChartSummary:
    """Render a single Phase T parameter chart and return the summary stats."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    ts_dt = pd.to_datetime(replay.timestamps_ns, unit="ns", utc=True)

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True,
        row_heights=[4, 3, 1, 2],
        vertical_spacing=0.04,
        subplot_titles=(
            "Tick Price + Phase S vs simulated EXIT_D",
            "Intensity ratio I(t)",
            "EPG state",
            "Per-trade outcome (orig vs EXIT_D)",
        ),
    )

    # ── Row 1: tick price ──
    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=replay.prices, mode="lines",
            line=dict(color="#444444", width=1),
            name="price", hoverinfo="skip", showlegend=False,
        ), row=1, col=1,
    )

    # PASS-window vrects on rows 1 + 2
    for i in range(len(replay.pass_window_open_ts)):
        x0 = pd.Timestamp(replay.pass_window_open_ts[i], unit="ns", tz="UTC")
        x1 = pd.Timestamp(replay.pass_window_close_ts[i], unit="ns", tz="UTC")
        for r in (1, 2):
            fig.add_vrect(x0=x0, x1=x1, fillcolor="rgba(0,200,80,0.10)",
                          line_width=0, layer="below", row=r, col=1)

    # Phase S entries
    if len(phase_s_trades) > 0:
        ent_dt = pd.to_datetime(
            phase_s_trades["entry_ts"].astype("int64").values,
            unit="ns", utc=True,
        )
        fig.add_trace(
            go.Scatter(
                x=ent_dt, y=phase_s_trades["entry_price"], mode="markers",
                marker=dict(symbol="triangle-up", size=12, color="#00CC44",
                            line=dict(color="#003311", width=1)),
                name="Phase S entry",
            ), row=1, col=1,
        )
        ex_dt = pd.to_datetime(
            phase_s_trades["exit_ts"].astype("int64").values,
            unit="ns", utc=True,
        )
        ex_colors = ["#00CC44" if p > 0 else "#FF3333"
                     for p in phase_s_trades["pnl_pct"]]
        fig.add_trace(
            go.Scatter(
                x=ex_dt, y=phase_s_trades["exit_price"], mode="markers",
                marker=dict(symbol="triangle-down", size=12, color=ex_colors,
                            line=dict(color="#222", width=1)),
                name="Phase S exit",
                customdata=phase_s_trades["pnl_pct"],
                hovertemplate="exit %{x|%H:%M:%S}<br>price %{y:.4f}"
                              "<br>pnl %{customdata:.3f}%<extra></extra>",
            ), row=1, col=1,
        )

    # Simulated EXIT_D fires
    fired = sim.exit_d_would_fire
    if fired.any():
        fdt = pd.to_datetime(sim.exit_d_ts[fired].astype("int64"),
                             unit="ns", utc=True)
        fp_arr = sim.exit_d_price[fired]
    else:
        fdt = pd.to_datetime([], unit="ns", utc=True)
        fp_arr = np.array([], dtype=np.float64)
    fig.add_trace(
        go.Scatter(
            x=fdt, y=fp_arr, mode="markers",
            marker=dict(symbol="diamond", size=12, color="#FFA500",
                        line=dict(color="#552200", width=1)),
            name="EXIT_D simulated",
        ), row=1, col=1,
    )

    # ── Row 2: I(t) + theta line ──
    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=replay.intensity_ratio, mode="lines",
            line=dict(color="#3366FF", width=1),
            name="I(t)", hoverinfo="skip", showlegend=False,
        ), row=2, col=1,
    )
    if len(ts_dt) > 0:
        fig.add_trace(
            go.Scatter(
                x=[ts_dt[0], ts_dt[-1]], y=[theta, theta], mode="lines",
                line=dict(color="#FF3333", dash="dash", width=1),
                name=f"θ={theta:.2f}", hoverinfo="skip",
            ), row=2, col=1,
        )

    # ── Row 3: EPG state ──
    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=replay.epg_state, mode="lines",
            line=dict(color="#888888", width=1, shape="hv"),
            name="state", hoverinfo="skip", showlegend=False,
        ), row=3, col=1,
    )

    # ── Row 4: per-trade outcome bars ──
    seqs = [str(int(s)) for s in sim.trade_seq]
    fig.add_trace(
        go.Bar(x=seqs, y=sim.original_pnl_pct,
               name="original pnl_pct", marker_color="#888888"),
        row=4, col=1,
    )
    ed_pnl = np.where(fired, sim.exit_d_pnl_pct, 0.0)
    fig.add_trace(
        go.Bar(x=seqs, y=ed_pnl,
               name="EXIT_D pnl_pct", marker_color="#FFA500"),
        row=4, col=1,
    )

    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="I(t)", range=[0, 1], row=2, col=1)
    fig.update_yaxes(
        title_text="EPG state", range=[-0.5, 3.5],
        tickvals=[0, 1, 2, 3],
        ticktext=["INACTIVE", "WARMUP", "PASS", "FAIL"],
        row=3, col=1,
    )
    fig.update_yaxes(title_text="pnl_pct", row=4, col=1)
    fig.update_xaxes(title_text="Time (UTC)", row=3, col=1)

    summary = _summarize(sim, theta, tau_min_sec, chart_filename=output_path.name)

    title = (f"θ={theta:.2f}  τ_min={tau_min_sec:.1f}s  |  "
             f"Δ={summary.sum_delta_pnl_pct:+.2f}%  |  "
             f"{summary.n_exit_d_fired}/{summary.n_trades} fired")
    fig.update_layout(
        title=title, height=1100, template="plotly_white",
        barmode="group", margin=dict(t=80, l=60, r=20, b=40),
    )

    fig.write_html(str(output_path), include_plotlyjs="cdn")
    return summary
