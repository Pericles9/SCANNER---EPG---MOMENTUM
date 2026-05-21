"""Phase E per-event chart builder -- symmetric spread-multiple LULD exit (4-panel).

Panel layout (shared x-axis):
    1. 10s OHLCV candlesticks + entry/exit markers + LULD trigger bands
       + current_window_high step line
    2. Sell intensity I(t) + EXIT_D fire markers
    3. Intra-window drawdown from rolling high + threshold line + blocked markers
    4. EPG state coloured band strip

LULD band lines in Panel 1:
    lower trigger = lower_band + N * spread  (red dashed)
    upper trigger = upper_band - N * spread  (orange dashed)
    lower_band itself                         (red dotted, lighter)
    upper_band itself                         (orange dotted, lighter)

Exit markers in Panel 1:
    luld_lower fires: red X
    luld_upper fires: orange X
    other exits: triangle-down (green/red by PnL)

Public API:
    build_chart(ticker, date, replay, trades_df, blocked_df,
                threshold, n_spread_multiple, qd) -> go.Figure
"""
from __future__ import annotations

from pathlib import Path
import sys
from typing import Optional

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tools.exit_d_tuning.replay import EventReplay
from core.exits.luld_proximity import LuldProximityExit, ProximityState

_THETA = 0.65

_STATE_COLORS = {
    0: "#CCCCCC",
    1: "#FFEE66",
    2: "#00CC44",
    3: "#FF3333",
}

_PASS_FILL = "rgba(0,200,80,0.10)"

_LULD_LOWER_COLOR = "#CC2200"       # red -- lower band / lower fire
_LULD_UPPER_COLOR = "#FF6600"       # orange -- upper band / upper fire
_LULD_LOWER_BAND_COLOR = "rgba(204,34,0,0.35)"
_LULD_UPPER_BAND_COLOR = "rgba(255,102,0,0.35)"


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


def _compute_luld_bands(
    replay: EventReplay,
    qd,
    n_spread_multiple: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """Replay LULD proximity to build band arrays aligned with replay ticks.

    Returns:
        lower_band_arr    -- LULD lower band at each tick (NaN if INACTIVE)
        upper_band_arr    -- LULD upper band at each tick (NaN if INACTIVE)
        lower_trigger_arr -- lower_band + N*spread (NaN if INACTIVE)
        upper_trigger_arr -- upper_band - N*spread (NaN if INACTIVE)
    """
    N = len(replay.timestamps_ns)
    lower_band_arr = np.full(N, np.nan)
    upper_band_arr = np.full(N, np.nan)
    lower_trigger_arr = np.full(N, np.nan)
    upper_trigger_arr = np.full(N, np.nan)

    if qd is None:
        return lower_band_arr, upper_band_arr, lower_trigger_arr, upper_trigger_arr

    luld = LuldProximityExit(
        ref_window_sec=300.0,
        n_spread_multiple=n_spread_multiple,
        warmup_sec=60.0,
    )

    nq = qd.n_quotes
    q_idx = 0

    for i in range(N):
        ts_i = int(replay.timestamps_ns[i])

        while q_idx < nq - 1 and qd.timestamps[q_idx + 1] <= ts_i:
            q_idx += 1

        if q_idx < nq and qd.timestamps[q_idx] <= ts_i:
            bid = float(qd.bid_prices[q_idx])
            ask = float(qd.ask_prices[q_idx])
            if bid <= 0.0 or ask <= bid:
                bid = ask = None
        else:
            bid = ask = None

        lr = luld.update(ts_i, float(replay.prices[i]), bid, ask)

        if lr.lower_band is not None:
            lower_band_arr[i] = lr.lower_band
            upper_band_arr[i] = lr.upper_band
            spread = lr.spread_used if lr.spread_used is not None else 0.0
            buf = n_spread_multiple * spread
            lower_trigger_arr[i] = lr.lower_band + buf
            upper_trigger_arr[i] = lr.upper_band - buf

    return lower_band_arr, upper_band_arr, lower_trigger_arr, upper_trigger_arr


def build_chart(
    ticker: str,
    date: str,
    replay: EventReplay,
    trades_df: pd.DataFrame,
    blocked_df: pd.DataFrame,
    threshold: float = 0.02,
    n_spread_multiple: float = 2.0,
    qd=None,
) -> go.Figure:
    """Build a 4-panel Phase E per-event chart."""
    ts_dt = pd.to_datetime(replay.timestamps_ns, unit="ns", utc=True)
    ohlcv = _build_ohlcv_10s(replay.timestamps_ns, replay.prices)
    cwh_arr, drawdown_arr, window_reset_ts = _compute_intra_window_state(replay)
    lower_band, upper_band, lower_trig, upper_trig = _compute_luld_bands(
        replay, qd, n_spread_multiple
    )

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

    exit_reasons = trades_df["exit_reason"].values if len(trades_df) else np.array([])
    n_luld_lower = int(np.sum(exit_reasons == "luld_lower"))
    n_luld_upper = int(np.sum(exit_reasons == "luld_upper"))

    title = (
        f"{ticker} {date} -- Phase E LULD N={n_spread_multiple:.0f} | "
        f"n={len(trades_df)} ({n_first}f+{n_reentry}re) | "
        f"{n_blocks_total} blocked | "
        f"luld_lo={n_luld_lower} luld_hi={n_luld_upper} | "
        f"PF={pf_str} | wm={threshold:.0%} | {session}"
    )

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[4, 2.5, 2.5, 1],
        vertical_spacing=0.03,
        subplot_titles=[
            "Price + LULD Bands + Window High",
            "I(t) sell",
            "Intra-Window Drawdown",
            "EPG state",
        ],
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

    # ── Panel 1: Price + LULD bands + cwh step line ──────────────────────────

    # LULD raw bands (lighter, dotted)
    lb_valid = ~np.isnan(lower_band)
    if lb_valid.any():
        fig.add_trace(
            go.Scatter(
                x=ts_dt[lb_valid], y=lower_band[lb_valid],
                mode="lines",
                line=dict(color=_LULD_LOWER_BAND_COLOR, width=1, dash="dot"),
                name="LULD lower band",
                hovertemplate="lower_band %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=ts_dt[lb_valid], y=upper_band[lb_valid],
                mode="lines",
                line=dict(color=_LULD_UPPER_BAND_COLOR, width=1, dash="dot"),
                name="LULD upper band",
                hovertemplate="upper_band %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # LULD triggers (solid + buffer, more visible)
    lt_valid = ~np.isnan(lower_trig)
    if lt_valid.any():
        fig.add_trace(
            go.Scatter(
                x=ts_dt[lt_valid], y=lower_trig[lt_valid],
                mode="lines",
                line=dict(color=_LULD_LOWER_COLOR, width=1.5, dash="dash"),
                name=f"lower trigger (N={n_spread_multiple:.0f})",
                hovertemplate="lower_trigger %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=ts_dt[lt_valid], y=upper_trig[lt_valid],
                mode="lines",
                line=dict(color=_LULD_UPPER_COLOR, width=1.5, dash="dash"),
                name=f"upper trigger (N={n_spread_multiple:.0f})",
                hovertemplate="upper_trigger %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )

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

    # current_window_high step line
    cwh_valid = ~np.isnan(cwh_arr)
    if cwh_valid.any():
        fig.add_trace(
            go.Scatter(
                x=ts_dt[cwh_valid], y=cwh_arr[cwh_valid],
                mode="lines",
                line=dict(color="#9933CC", width=1.5, dash="dash"),
                name="window_high",
                hovertemplate="win_high %{x|%H:%M:%S}<br>%{y:.4f}<extra></extra>",
            ),
            row=1, col=1,
        )

    # Window reset markers
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

        # Non-LULD exits (triangles)
        non_luld = ~np.isin(exit_reasons, ["luld_lower", "luld_upper"])
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

        # LULD lower fires (red X)
        luld_lo_mask = exit_reasons == "luld_lower"
        if luld_lo_mask.any():
            custom_lo = np.column_stack([pnl[luld_lo_mask]])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[luld_lo_mask], y=exit_prices[luld_lo_mask],
                    mode="markers",
                    marker=dict(symbol="x", size=12, color=_LULD_LOWER_COLOR,
                                line=dict(color="#660000", width=2)),
                    name="LULD lower exit",
                    customdata=custom_lo,
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<br>pnl %{customdata[0]:.3f}%<br>"
                        "exit: LULD lower<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        # LULD upper fires (orange X)
        luld_hi_mask = exit_reasons == "luld_upper"
        if luld_hi_mask.any():
            custom_hi = np.column_stack([pnl[luld_hi_mask]])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[luld_hi_mask], y=exit_prices[luld_hi_mask],
                    mode="markers",
                    marker=dict(symbol="x", size=12, color=_LULD_UPPER_COLOR,
                                line=dict(color="#662200", width=2)),
                    name="LULD upper exit",
                    customdata=custom_hi,
                    hovertemplate=(
                        f"{ticker} {date}<br>%{{x|%H:%M:%S}}<br>"
                        "price %{y:.4f}<br>pnl %{customdata[0]:.3f}%<br>"
                        "exit: LULD upper<extra></extra>"
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

    # ── Panel 2: Sell intensity I(t) ─────────────────────────────────────────
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

    # ── Panel 3: Intra-window drawdown ───────────────────────────────────────
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

    # ── Layout ───────────────────────────────────────────────────────────────
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
