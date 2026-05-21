"""Phase C per-event chart builder (4-panel layout).

Identical to Phase B except Panel 3 substitutes CVD since T_event
for I_buy(t), showing the cumulative dollar-volume imbalance used
by the CVD filter.

Public API:
    build_chart(ticker, date, replay, sizes, trades_df, event_meta) -> go.Figure

Panel layout (shared x-axis):
    1. 10s OHLCV candlesticks + entry/exit markers
    2. Sell intensity I(t) + EXIT_D fire markers
    3. CVD since T_event (dollar-weighted, Lee-Ready sides) + entry markers
    4. EPG state coloured band strip
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.exit_d_tuning.replay import EventReplay

_THETA = 0.65

_STATE_COLORS = {
    0: "#CCCCCC",
    1: "#FFEE66",
    2: "#00CC44",
    3: "#FF3333",
}

_PASS_FILL = "rgba(0,200,80,0.10)"


def _build_ohlcv_10s(timestamps_ns: np.ndarray, prices: np.ndarray) -> pd.DataFrame:
    ts = pd.to_datetime(timestamps_ns, unit="ns", utc=True)
    s = pd.Series(prices, index=ts, dtype=float)
    ohlcv = s.resample("10s").ohlc().dropna()
    return ohlcv


def _add_pass_bands(fig: go.Figure, replay: EventReplay, rows: list[int]) -> None:
    for i in range(len(replay.pass_window_open_ts)):
        x0 = pd.Timestamp(int(replay.pass_window_open_ts[i]), unit="ns", tz="UTC")
        x1 = pd.Timestamp(int(replay.pass_window_close_ts[i]), unit="ns", tz="UTC")
        for r in rows:
            fig.add_vrect(x0=x0, x1=x1, fillcolor=_PASS_FILL,
                          line_width=0, layer="below", row=r, col=1)


def _state_runs(epg_state: np.ndarray, timestamps_ns: np.ndarray):
    if len(epg_state) == 0:
        return
    cur_state = epg_state[0]
    run_start = 0
    for i in range(1, len(epg_state)):
        if epg_state[i] != cur_state:
            yield timestamps_ns[run_start], timestamps_ns[i - 1], int(cur_state)
            cur_state = epg_state[i]
            run_start = i
    yield timestamps_ns[run_start], timestamps_ns[-1], int(cur_state)


def _compute_cvd(replay: EventReplay, sizes: np.ndarray) -> np.ndarray:
    """Compute cumulative dollar-volume CVD from T_event onwards.

    Returns array of same length as replay, NaN before T_event fires.
    """
    N = len(replay.timestamps_ns)
    cvd = np.full(N, np.nan)

    if replay.t_event_ns is None:
        return cvd

    t_event_idx = int(np.searchsorted(replay.timestamps_ns, replay.t_event_ns,
                                       side="left"))
    if t_event_idx >= N:
        return cvd

    running = 0.0
    for i in range(t_event_idx, N):
        direction = 1.0 if replay.sides[i] == 1 else -1.0
        running += float(replay.prices[i]) * float(sizes[i]) * direction
        cvd[i] = running
    return cvd


def build_chart(
    ticker: str,
    date: str,
    replay: EventReplay,
    sizes: np.ndarray,
    trades_df: pd.DataFrame,
    event_meta: dict,
) -> go.Figure:
    """Build a 4-panel Phase C per-event chart with CVD in panel 3."""
    ts_dt = pd.to_datetime(replay.timestamps_ns, unit="ns", utc=True)
    ohlcv = _build_ohlcv_10s(replay.timestamps_ns, replay.prices)
    cvd = _compute_cvd(replay, sizes)

    n_first = int((trades_df["entry_type"] == "first").sum()) if len(trades_df) else 0
    n_reentry = int((trades_df["entry_type"] == "reentry").sum()) if len(trades_df) else 0
    n_cvd_blocks = event_meta.get("n_cvd_blocks", 0)

    wins = trades_df.loc[trades_df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(trades_df.loc[trades_df["pnl_pct"] < 0, "pnl_pct"].sum())
    event_pf = wins / losses if losses > 0 else float("nan")
    pf_str = f"{event_pf:.4f}" if not np.isnan(event_pf) else "N/A"

    session = trades_df["session_bucket"].iloc[0] if len(trades_df) else "unknown"

    title = (
        f"{ticker} {date} — Phase C CVD | "
        f"{n_first} first + {n_reentry} re-entries | "
        f"{n_cvd_blocks} CVD-blocked | "
        f"PF={pf_str} | {session}"
    )

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[4, 2.5, 2.5, 1],
        vertical_spacing=0.03,
        subplot_titles=["Price", "I(t) sell", "CVD since T_event", "EPG state"],
    )

    # Invisible anchor traces so add_vrect can resolve y-axis refs
    _anchor_x = ts_dt[:1] if len(ts_dt) else pd.to_datetime([], utc=True)
    for _r in [1, 2, 3, 4]:
        fig.add_trace(
            go.Scatter(x=_anchor_x, y=[0.5] if len(_anchor_x) else [],
                       mode="markers", marker=dict(opacity=0),
                       showlegend=False, hoverinfo="skip"),
            row=_r, col=1,
        )

    _add_pass_bands(fig, replay, rows=[1, 2, 3])

    # EPG state coloured bands (panel 4)
    for start_ns, end_ns, state_int in _state_runs(replay.epg_state, replay.timestamps_ns):
        x0 = pd.Timestamp(int(start_ns), unit="ns", tz="UTC")
        x1 = pd.Timestamp(int(end_ns), unit="ns", tz="UTC")
        color_hex = _STATE_COLORS.get(state_int, "#CCCCCC")
        r, g, b = (int(color_hex[1:3], 16), int(color_hex[3:5], 16),
                   int(color_hex[5:7], 16))
        fig.add_vrect(x0=x0, x1=x1, fillcolor=f"rgba({r},{g},{b},0.4)",
                      line_width=0, layer="below", row=4, col=1)

    # T_event vline (all panels)
    if replay.t_event_ns is not None:
        t_event_dt = pd.Timestamp(int(replay.t_event_ns), unit="ns", tz="UTC")
        for r in [1, 2, 3, 4]:
            fig.add_vline(x=t_event_dt,
                          line=dict(color="#FFA500", width=1, dash="dot"),
                          row=r, col=1)

    # ── Panel 1: Price ──────────────────────────────────────────────────────
    if len(ohlcv) > 0:
        fig.add_trace(
            go.Candlestick(
                x=ohlcv.index,
                open=ohlcv["open"], high=ohlcv["high"],
                low=ohlcv["low"], close=ohlcv["close"],
                name="10s OHLCV",
                increasing_line_color="#00AA44",
                decreasing_line_color="#CC2200",
                showlegend=False,
            ),
            row=1, col=1,
        )

    if len(trades_df) > 0:
        entry_ts_arr = trades_df["entry_ts"].astype("int64").values
        ent_dt = pd.to_datetime(entry_ts_arr, unit="ns", utc=True)

        first_mask = trades_df["entry_type"] == "first"
        if first_mask.any():
            # Color first entries by CVD sign at entry time
            first_ent_ts = trades_df.loc[first_mask, "entry_ts"].astype("int64").values
            cvd_at_entry_vals = trades_df.loc[first_mask, "cvd_at_entry"].values
            marker_colors = ["#00CC44" if c >= 0 else "#CC2200"
                             for c in cvd_at_entry_vals]
            fig.add_trace(
                go.Scatter(
                    x=pd.to_datetime(first_ent_ts, unit="ns", utc=True),
                    y=trades_df.loc[first_mask, "entry_price"],
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=12, color=marker_colors,
                                line=dict(color="#003311", width=1)),
                    name="First entry",
                    customdata=cvd_at_entry_vals[:, None],
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<br>cvd_at_entry %{customdata[0]:.0f}<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        re_mask = trades_df["entry_type"] == "reentry"
        if re_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ent_dt[re_mask],
                    y=trades_df.loc[re_mask, "entry_price"],
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=10, color="#3399FF",
                                line=dict(color="#002266", width=1)),
                    name="Re-entry",
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        # Exit markers
        exit_ts_arr = trades_df["exit_ts"].astype("int64").values
        ex_dt = pd.to_datetime(exit_ts_arr, unit="ns", utc=True)
        pnl = trades_df["pnl_pct"].values
        exit_prices = trades_df["exit_price"].values
        exit_reasons = trades_df["exit_reason"].values
        entry_types = trades_df["entry_type"].values

        non_luld = exit_reasons != "luld_proximity"
        if non_luld.any():
            colors = ["#00CC44" if p > 0 else ("#CC2200" if p < 0 else "#888888")
                      for p in pnl[non_luld]]
            custom = np.column_stack([pnl[non_luld], exit_reasons[non_luld],
                                      entry_types[non_luld]])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[non_luld], y=exit_prices[non_luld],
                    mode="markers",
                    marker=dict(symbol="triangle-down", size=10, color=colors,
                                line=dict(color="#222222", width=1)),
                    name="Exit",
                    customdata=custom,
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<br>pnl %{customdata[0]:.3f}%<br>"
                        "exit: %{customdata[1]}<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        luld_mask = exit_reasons == "luld_proximity"
        if luld_mask.any():
            custom_l = np.column_stack([pnl[luld_mask], exit_reasons[luld_mask],
                                        entry_types[luld_mask]])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[luld_mask], y=exit_prices[luld_mask],
                    mode="markers",
                    marker=dict(symbol="x", size=12, color="#FF6600",
                                line=dict(color="#662200", width=2)),
                    name="LULD exit",
                    customdata=custom_l,
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<br>pnl %{customdata[0]:.3f}%<br>"
                        "exit: LULD proximity<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

    # ── Panel 2: Sell intensity I(t) ────────────────────────────────────────
    i_sell = replay.intensity_ratio
    fig.add_trace(
        go.Scatter(x=ts_dt, y=i_sell, mode="lines",
                   line=dict(color="#3366FF", width=1),
                   name="I(t) sell", hoverinfo="skip"),
        row=2, col=1,
    )
    if len(ts_dt) > 0:
        fig.add_trace(
            go.Scatter(x=[ts_dt[0], ts_dt[-1]], y=[_THETA, _THETA],
                       mode="lines",
                       line=dict(color="#FF6600", dash="dash", width=1.5),
                       name=f"theta={_THETA:.2f}", hoverinfo="skip"),
            row=2, col=1,
        )

    if len(trades_df) > 0:
        exitd_mask = trades_df["exit_reason"] == "exit_d"
        if exitd_mask.any():
            ex_d_ts = trades_df.loc[exitd_mask, "exit_ts"].astype("int64").values
            ex_d_dt = pd.to_datetime(ex_d_ts, unit="ns", utc=True)
            idx_nearest = np.clip(
                np.searchsorted(replay.timestamps_ns, ex_d_ts, side="left"),
                0, len(replay.intensity_ratio) - 1,
            )
            i_at_exit = replay.intensity_ratio[idx_nearest]
            fig.add_trace(
                go.Scatter(
                    x=ex_d_dt, y=i_at_exit, mode="markers",
                    marker=dict(symbol="diamond", size=12, color="#FF6600",
                                line=dict(color="#552200", width=1)),
                    name="EXIT_D fire",
                    hovertemplate="EXIT_D<br>%{x|%H:%M:%S}<br>I(t)=%{y:.3f}<extra></extra>",
                ),
                row=2, col=1,
            )

    # ── Panel 3: CVD since T_event ──────────────────────────────────────────
    valid = ~np.isnan(cvd)
    if valid.any():
        cvd_ts = ts_dt[valid]
        cvd_vals = cvd[valid]

        # Split into positive/negative segments for coloring
        fig.add_trace(
            go.Scatter(
                x=cvd_ts, y=cvd_vals, mode="lines",
                line=dict(color="#9933CC", width=1.5),
                name="CVD since T_event",
                hovertemplate="CVD %{x|%H:%M:%S}<br>%{y:.0f}<extra></extra>",
            ),
            row=3, col=1,
        )

    # Zero reference line in Panel 3
    if valid.any():
        fig.add_trace(
            go.Scatter(
                x=[ts_dt[valid][0], ts_dt[valid][-1]], y=[0, 0],
                mode="lines",
                line=dict(color="#888888", dash="dash", width=1),
                name="CVD=0 threshold", hoverinfo="skip",
            ),
            row=3, col=1,
        )

    # First-entry dots on Panel 3 (color by CVD sign)
    if len(trades_df) > 0:
        first_mask = trades_df["entry_type"] == "first"
        if first_mask.any():
            first_ent_ts = trades_df.loc[first_mask, "entry_ts"].astype("int64").values
            cvd_at_entry_vals = trades_df.loc[first_mask, "cvd_at_entry"].values
            marker_colors = ["#00CC44" if c >= 0 else "#CC2200"
                             for c in cvd_at_entry_vals]
            fig.add_trace(
                go.Scatter(
                    x=pd.to_datetime(first_ent_ts, unit="ns", utc=True),
                    y=cvd_at_entry_vals,
                    mode="markers",
                    marker=dict(symbol="diamond", size=10, color=marker_colors,
                                line=dict(color="#333333", width=1)),
                    name="First entry CVD",
                    hovertemplate=(
                        "Entry CVD<br>%{x|%H:%M:%S}<br>cvd=%{y:.0f}<extra></extra>"
                    ),
                ),
                row=3, col=1,
            )

    # ── Layout ──────────────────────────────────────────────────────────────
    fig.update_layout(
        title=title,
        width=1400,
        height=900,
        template="plotly_white",
        margin=dict(t=70, l=60, r=20, b=40),
        legend=dict(orientation="h", y=-0.04, x=0),
        xaxis_rangeslider_visible=False,
    )
    fig.update_layout(xaxis=dict(rangeslider=dict(visible=False)))

    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="I(t) sell", range=[0, 1], row=2, col=1)
    fig.update_yaxes(title_text="CVD ($)", row=3, col=1)
    fig.update_yaxes(
        title_text="EPG state", range=[0, 1], showticklabels=False, row=4, col=1,
    )

    return fig
