"""Phase D per-event chart builder — intra-window rolling high watermark (4-panel).

Panel layout (shared x-axis):
    1. 10s OHLCV candlesticks + entry/exit markers + current_window_high step line
    2. Sell intensity I(t) + EXIT_D fire markers
    3. Intra-window drawdown from rolling high + threshold line + blocked entry markers
    4. EPG state coloured band strip

Public API:
    build_chart(ticker, date, replay, trades_df, blocked_df, threshold) -> go.Figure
"""
from __future__ import annotations

from pathlib import Path
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
    return s.resample("10s").ohlc().dropna()


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


def _compute_intra_window_state(replay: EventReplay) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Compute intra-window rolling high and drawdown arrays.

    Returns:
        cwh_arr: current_window_high at each tick (NaN outside PASS windows)
        drawdown_arr: drawdown from cwh at each tick (NaN outside PASS)
        window_reset_ts: array of PASS window open timestamps (ns)
    """
    N = len(replay.timestamps_ns)
    cwh_arr = np.full(N, np.nan)
    drawdown_arr = np.full(N, np.nan)

    n_windows = len(replay.pass_window_open_ts)
    if n_windows == 0:
        return cwh_arr, drawdown_arr, np.array([], dtype=np.int64)

    window_reset_ts = np.array([int(t) for t in replay.pass_window_open_ts], dtype=np.int64)
    prior_peak: float | None = None

    for w in range(n_windows):
        w_open = int(replay.pass_window_open_ts[w])
        w_close = int(replay.pass_window_close_ts[w])

        i_start = int(np.searchsorted(replay.timestamps_ns, w_open, side="left"))
        i_end = int(np.searchsorted(replay.timestamps_ns, w_close, side="right"))
        i_end = min(i_end, N)

        if i_start >= N:
            break

        cur_p = float(replay.prices[i_start])
        cwh = max(cur_p, prior_peak) if prior_peak is not None else cur_p

        for i in range(i_start, i_end):
            p = float(replay.prices[i])
            if p > cwh:
                cwh = p
            cwh_arr[i] = cwh
            drawdown_arr[i] = max(0.0, (cwh - p) / cwh) if cwh > 0 else 0.0

        prior_peak = cwh

    return cwh_arr, drawdown_arr, window_reset_ts


def build_chart(
    ticker: str,
    date: str,
    replay: EventReplay,
    trades_df: pd.DataFrame,
    blocked_df: pd.DataFrame,
    threshold: float = 0.02,
) -> go.Figure:
    """Build a 4-panel Phase D intra-window watermark per-event chart."""
    ts_dt = pd.to_datetime(replay.timestamps_ns, unit="ns", utc=True)
    ohlcv = _build_ohlcv_10s(replay.timestamps_ns, replay.prices)
    cwh_arr, drawdown_arr, window_reset_ts = _compute_intra_window_state(replay)

    n_first = int((trades_df["entry_type"] == "first").sum()) if len(trades_df) else 0
    n_reentry = int((trades_df["entry_type"] == "reentry").sum()) if len(trades_df) else 0
    n_first_blocked = int((blocked_df["entry_type"] == "first").sum()) if len(blocked_df) else 0
    n_re_blocked = int((blocked_df["entry_type"] == "reentry").sum()) if len(blocked_df) else 0
    n_blocks_total = n_first_blocked + n_re_blocked

    wins = trades_df.loc[trades_df["pnl_pct"] > 0, "pnl_pct"].sum() if len(trades_df) else 0
    losses = abs(trades_df.loc[trades_df["pnl_pct"] < 0, "pnl_pct"].sum()) if len(trades_df) else 0
    event_pf = wins / losses if losses > 0 else float("nan")
    pf_str = f"{event_pf:.4f}" if not np.isnan(event_pf) else "N/A"
    session = trades_df["session_bucket"].iloc[0] if len(trades_df) else "unknown"

    title = (
        f"{ticker} {date} — Phase D Intra-Window WM | "
        f"n={len(trades_df)} ({n_first} first + {n_reentry} re-entry) | "
        f"{n_blocks_total} blocked ({n_first_blocked} first + {n_re_blocked} re) | "
        f"PF={pf_str} | wm={threshold:.0%} | {session}"
    )

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[4, 2.5, 2.5, 1],
        vertical_spacing=0.03,
        subplot_titles=["Price + Window High", "I(t) sell", "Intra-Window Drawdown", "EPG state"],
    )

    # Anchor traces for vrect row-binding
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
        r, g, b = (int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16))
        fig.add_vrect(x0=x0, x1=x1, fillcolor=f"rgba({r},{g},{b},0.4)",
                      line_width=0, layer="below", row=4, col=1)

    # T_event vertical line (all panels)
    if replay.t_event_ns is not None:
        t_event_dt = pd.Timestamp(int(replay.t_event_ns), unit="ns", tz="UTC")
        for r in [1, 2, 3, 4]:
            fig.add_vline(x=t_event_dt,
                          line=dict(color="#FFA500", width=1, dash="dot"),
                          row=r, col=1)

    # ── Panel 1: Price + current_window_high step line ──────────────────────
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

    # current_window_high step line (only where defined)
    cwh_valid = ~np.isnan(cwh_arr)
    if cwh_valid.any():
        fig.add_trace(
            go.Scatter(
                x=ts_dt[cwh_valid],
                y=cwh_arr[cwh_valid],
                mode="lines",
                line=dict(color="#9933CC", width=1.5, dash="dash"),
                name="window_high",
                hovertemplate="win_high %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Window reset markers (small ticks at top of Panel 1)
    if len(window_reset_ts) > 0:
        reset_dt = pd.to_datetime(window_reset_ts, unit="ns", utc=True)
        reset_idx = np.clip(
            np.searchsorted(replay.timestamps_ns, window_reset_ts, side="left"),
            0, len(replay.prices) - 1,
        )
        reset_prices = replay.prices[reset_idx]
        fig.add_trace(
            go.Scatter(
                x=reset_dt, y=reset_prices,
                mode="markers",
                marker=dict(symbol="diamond", size=8, color="#9933CC",
                            line=dict(color="#330066", width=1)),
                name="Window reset",
                hovertemplate="Win reset<br>%{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )

    if len(trades_df) > 0:
        entry_ts_arr = trades_df["entry_ts"].astype("int64").values
        ent_dt = pd.to_datetime(entry_ts_arr, unit="ns", utc=True)

        first_mask = (trades_df["entry_type"] == "first").values
        if first_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ent_dt[first_mask],
                    y=trades_df.loc[first_mask, "entry_price"].values,
                    mode="markers",
                    marker=dict(symbol="triangle-up", size=12, color="#00CC44",
                                line=dict(color="#003311", width=1)),
                    name="First entry",
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        re_mask = (trades_df["entry_type"] == "reentry").values
        if re_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ent_dt[re_mask],
                    y=trades_df.loc[re_mask, "entry_price"].values,
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

        exit_ts_arr = trades_df["exit_ts"].astype("int64").values
        ex_dt = pd.to_datetime(exit_ts_arr, unit="ns", utc=True)
        pnl = trades_df["pnl_pct"].values
        exit_prices = trades_df["exit_price"].values
        exit_reasons = trades_df["exit_reason"].values

        non_luld = exit_reasons != "luld_proximity"
        if non_luld.any():
            colors = ["#00CC44" if p > 0 else ("#CC2200" if p < 0 else "#888888")
                      for p in pnl[non_luld]]
            custom = np.column_stack([pnl[non_luld], exit_reasons[non_luld]])
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
            custom_l = np.column_stack([pnl[luld_mask], exit_reasons[luld_mask]])
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

    # Blocked entry markers on Panel 1
    if len(blocked_df) > 0:
        blocked_ts_arr = blocked_df["entry_ts"].astype("int64").values
        b_idx = np.clip(
            np.searchsorted(replay.timestamps_ns, blocked_ts_arr, side="left"),
            0, len(replay.prices) - 1,
        )
        b_prices = replay.prices[b_idx]
        b_dfw = blocked_df["drawdown_from_window_high"].values
        b_type = blocked_df["entry_type"].values
        b_dt = pd.to_datetime(blocked_ts_arr, unit="ns", utc=True)

        for etype, color, outline, label in [
            ("first", "#CC2200", "#660000", "Blocked first"),
            ("reentry", "#FF6600", "#662200", "Blocked re-entry"),
        ]:
            mask = b_type == etype
            if mask.any():
                custom_b = np.column_stack([b_dfw[mask]])
                fig.add_trace(
                    go.Scatter(
                        x=b_dt[mask], y=b_prices[mask],
                        mode="markers",
                        marker=dict(symbol="x", size=11, color=color,
                                    line=dict(color=outline, width=2)),
                        name=label,
                        customdata=custom_b,
                        hovertemplate=(
                            f"{label}<br>%{{x|%H:%M:%S}}<br>"
                            "price %{y:.4f}<br>dfw %{customdata[0]:.2%}<extra></extra>"
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

    # ── Panel 3: Intra-window drawdown ──────────────────────────────────────
    dw_valid = ~np.isnan(drawdown_arr)
    if dw_valid.any():
        fig.add_trace(
            go.Scatter(
                x=ts_dt[dw_valid], y=drawdown_arr[dw_valid],
                mode="lines",
                line=dict(color="#9933CC", width=1.5),
                name="Intra-window drawdown",
                hovertemplate="Drawdown %{x|%H:%M:%S}<br>%{y:.2%}<extra></extra>",
            ),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=[ts_dt[dw_valid][0], ts_dt[dw_valid][-1]],
                y=[threshold, threshold],
                mode="lines",
                line=dict(color="#CC2200", dash="dash", width=1.5),
                name=f"WM threshold {threshold:.0%}", hoverinfo="skip",
            ),
            row=3, col=1,
        )

    # Blocked entry markers on Panel 3 (show at their drawdown level)
    if len(blocked_df) > 0:
        blocked_ts_arr = blocked_df["entry_ts"].astype("int64").values
        b_idx = np.clip(
            np.searchsorted(replay.timestamps_ns, blocked_ts_arr, side="left"),
            0, len(drawdown_arr) - 1,
        )
        b_dfw_raw = blocked_df["drawdown_from_window_high"].values
        b_dfw_plot = np.where(np.isnan(drawdown_arr[b_idx]), threshold, b_dfw_raw)
        b_type = blocked_df["entry_type"].values
        b_dt = pd.to_datetime(blocked_ts_arr, unit="ns", utc=True)

        for etype, color, outline, label in [
            ("first", "#CC2200", "#660000", "Blocked first"),
            ("reentry", "#FF6600", "#662200", "Blocked re-entry"),
        ]:
            mask = b_type == etype
            if mask.any():
                fig.add_trace(
                    go.Scatter(
                        x=b_dt[mask], y=b_dfw_plot[mask],
                        mode="markers",
                        marker=dict(symbol="x", size=12, color=color,
                                    line=dict(color=outline, width=2)),
                        name=label,
                        showlegend=False,
                        hovertemplate=(
                            f"{label}<br>%{{x|%H:%M:%S}}<br>"
                            "dfw %{y:.2%}<extra></extra>"
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
    fig.update_yaxes(title_text="Drawdown", tickformat=".1%", row=3, col=1)
    fig.update_yaxes(title_text="EPG state", range=[0, 1],
                     showticklabels=False, row=4, col=1)

    return fig
