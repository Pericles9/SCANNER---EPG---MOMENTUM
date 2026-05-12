"""Phase B per-event chart builder (4-panel layout).

Public API:
    build_chart(ticker, date, replay, trades_df, event_meta) -> go.Figure

Panel layout (shared x-axis):
    1. 10s OHLCV candlesticks + entry/exit markers + LULD markers
    2. Sell intensity I(t) + EXIT_D fire markers
    3. Buy intensity I_buy(t) + re-entry fire markers
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
_BUY_THRESH = 1.0 - _THETA  # 0.35

# EPG state colours
_STATE_COLORS = {
    0: "#CCCCCC",  # INACTIVE
    1: "#FFEE66",  # WARMUP
    2: "#00CC44",  # PASS
    3: "#FF3333",  # FAIL
}
_STATE_LABELS = {0: "INACTIVE", 1: "WARMUP", 2: "PASS", 3: "FAIL"}

_PASS_FILL = "rgba(0,200,80,0.10)"


# ── helpers ──────────────────────────────────────────────────────────────────

def _build_ohlcv_10s(timestamps_ns: np.ndarray, prices: np.ndarray) -> pd.DataFrame:
    """Aggregate tick data into 10-second OHLCV bars."""
    ts = pd.to_datetime(timestamps_ns, unit="ns", utc=True)
    s = pd.Series(prices, index=ts, dtype=float)
    ohlcv = s.resample("10s").ohlc()
    ohlcv = ohlcv.dropna()
    return ohlcv


def _add_pass_bands(fig: go.Figure, replay: EventReplay, rows: list[int]) -> None:
    for i in range(len(replay.pass_window_open_ts)):
        x0 = pd.Timestamp(int(replay.pass_window_open_ts[i]), unit="ns", tz="UTC")
        x1 = pd.Timestamp(int(replay.pass_window_close_ts[i]), unit="ns", tz="UTC")
        for r in rows:
            fig.add_vrect(
                x0=x0, x1=x1,
                fillcolor=_PASS_FILL,
                line_width=0,
                layer="below",
                row=r, col=1,
            )


def _state_runs(epg_state: np.ndarray, timestamps_ns: np.ndarray):
    """Yield (start_ts, end_ts, state_int) for each contiguous run."""
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


# ── main chart builder ────────────────────────────────────────────────────────

def build_chart(
    ticker: str,
    date: str,
    replay: EventReplay,
    trades_df: pd.DataFrame,
    event_meta: dict,
) -> go.Figure:
    """Build a 4-panel Phase B per-event chart.

    Parameters
    ----------
    ticker, date : str
        Event identifiers.
    replay : EventReplay
        Output of replay_event_with_intensity().
    trades_df : pd.DataFrame
        Rows from per_trade.parquet filtered to this (ticker, date).
        Must have: entry_ts, exit_ts, entry_price, exit_price, pnl_pct,
        exit_reason, entry_type.
    event_meta : dict
        One entry from per_event_summary.json for this event.

    Returns
    -------
    go.Figure
        Fully configured interactive Plotly figure (not written to disk).
    """
    ts_dt = pd.to_datetime(replay.timestamps_ns, unit="ns", utc=True)
    ohlcv = _build_ohlcv_10s(replay.timestamps_ns, replay.prices)

    n_first = int((trades_df["entry_type"] == "first").sum()) if len(trades_df) else 0
    n_reentry = int((trades_df["entry_type"] == "reentry").sum()) if len(trades_df) else 0

    # Compute event PF
    wins = trades_df.loc[trades_df["pnl_pct"] > 0, "pnl_pct"].sum()
    losses = abs(trades_df.loc[trades_df["pnl_pct"] < 0, "pnl_pct"].sum())
    event_pf = wins / losses if losses > 0 else float("nan")
    pf_str = f"{event_pf:.4f}" if not np.isnan(event_pf) else "N/A"

    session = trades_df["session_bucket"].iloc[0] if len(trades_df) else "unknown"

    title = (
        f"{ticker} {date} — Phase B | "
        f"{n_first} first + {n_reentry} re-entries | "
        f"PF={pf_str} | {session}"
    )

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[4, 2.5, 2.5, 1],
        vertical_spacing=0.03,
        subplot_titles=["Price", "I(t) sell", "I_buy(t)", "EPG state"],
    )

    # Add invisible anchor traces to ALL rows immediately so that add_vrect
    # can resolve their y-axis references (required by Plotly before vrect calls).
    _anchor_x = ts_dt[:1] if len(ts_dt) else pd.to_datetime([], utc=True)
    for _r, _y in [(1, [0.5]), (2, [0.5]), (3, [0.5]), (4, [0.5])]:
        fig.add_trace(
            go.Scatter(x=_anchor_x, y=_y if len(_anchor_x) else [],
                       mode="markers", marker=dict(opacity=0),
                       showlegend=False, hoverinfo="skip"),
            row=_r, col=1,
        )

    # ── PASS bands across panels 1, 2, 3 (added before panel traces so they
    #    render below; axis refs already resolved by anchor traces above) ───
    _add_pass_bands(fig, replay, rows=[1, 2, 3])

    # ── EPG state coloured bands (panel 4) ───────────────────────────────
    for start_ns, end_ns, state_int in _state_runs(replay.epg_state, replay.timestamps_ns):
        x0 = pd.Timestamp(int(start_ns), unit="ns", tz="UTC")
        x1 = pd.Timestamp(int(end_ns), unit="ns", tz="UTC")
        color_hex = _STATE_COLORS.get(state_int, "#CCCCCC")
        r = int(color_hex[1:3], 16)
        g = int(color_hex[3:5], 16)
        b = int(color_hex[5:7], 16)
        fill = f"rgba({r},{g},{b},0.4)"
        fig.add_vrect(x0=x0, x1=x1, fillcolor=fill,
                      line_width=0, layer="below", row=4, col=1)

    # ── Panel 1: Price (candlesticks) ─────────────────────────────────────
    if len(ohlcv) > 0:
        fig.add_trace(
            go.Candlestick(
                x=ohlcv.index,
                open=ohlcv["open"],
                high=ohlcv["high"],
                low=ohlcv["low"],
                close=ohlcv["close"],
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

        # First entries (green triangle-up, size 12)
        first_mask = trades_df["entry_type"] == "first"
        if first_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ent_dt[first_mask],
                    y=trades_df.loc[first_mask, "entry_price"],
                    mode="markers",
                    marker=dict(
                        symbol="triangle-up", size=12, color="#00CC44",
                        line=dict(color="#003311", width=1),
                    ),
                    name="First entry",
                    hovertemplate=(
                        f"{ticker} {date}<br>"
                        "%{x|%H:%M:%S}<br>"
                        "price %{y:.4f}<br>"
                        "entry_type: first"
                        "<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        # Re-entries (blue triangle-up, size 10)
        re_mask = trades_df["entry_type"] == "reentry"
        if re_mask.any():
            fig.add_trace(
                go.Scatter(
                    x=ent_dt[re_mask],
                    y=trades_df.loc[re_mask, "entry_price"],
                    mode="markers",
                    marker=dict(
                        symbol="triangle-up", size=10, color="#3399FF",
                        line=dict(color="#002266", width=1),
                    ),
                    name="Re-entry",
                    hovertemplate=(
                        f"{ticker} {date}<br>"
                        "%{x|%H:%M:%S}<br>"
                        "price %{y:.4f}<br>"
                        "entry_type: reentry"
                        "<extra></extra>"
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

        # Non-LULD exits: green/red/grey triangle-down
        non_luld = exit_reasons != "luld_proximity"
        if non_luld.any():
            colors = []
            for p in pnl[non_luld]:
                if p > 0:
                    colors.append("#00CC44")
                elif p < 0:
                    colors.append("#CC2200")
                else:
                    colors.append("#888888")

            custom = np.column_stack([
                pnl[non_luld],
                exit_reasons[non_luld],
                entry_types[non_luld],
            ])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[non_luld],
                    y=exit_prices[non_luld],
                    mode="markers",
                    marker=dict(
                        symbol="triangle-down", size=10, color=colors,
                        line=dict(color="#222222", width=1),
                    ),
                    name="Exit",
                    customdata=custom,
                    hovertemplate=(
                        f"{ticker} {date}<br>"
                        "%{x|%H:%M:%S}<br>"
                        "price %{y:.4f}<br>"
                        "pnl %{customdata[0]:.3f}%<br>"
                        "exit: %{customdata[1]}<br>"
                        "type: %{customdata[2]}"
                        "<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

        # LULD exits: orange X
        luld_mask = exit_reasons == "luld_proximity"
        if luld_mask.any():
            custom_l = np.column_stack([
                pnl[luld_mask],
                exit_reasons[luld_mask],
                entry_types[luld_mask],
            ])
            fig.add_trace(
                go.Scatter(
                    x=ex_dt[luld_mask],
                    y=exit_prices[luld_mask],
                    mode="markers",
                    marker=dict(
                        symbol="x", size=12, color="#FF6600",
                        line=dict(color="#662200", width=2),
                    ),
                    name="LULD exit",
                    customdata=custom_l,
                    hovertemplate=(
                        f"{ticker} {date}<br>"
                        "%{x|%H:%M:%S}<br>"
                        "price %{y:.4f}<br>"
                        "pnl %{customdata[0]:.3f}%<br>"
                        "exit: LULD proximity<br>"
                        "type: %{customdata[2]}"
                        "<extra></extra>"
                    ),
                ),
                row=1, col=1,
            )

    # ── Panel 2: Sell intensity I(t) ──────────────────────────────────────
    i_sell = replay.intensity_ratio  # lambda_sell / total

    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=i_sell, mode="lines",
            line=dict(color="#3366FF", width=1),
            name="I(t) sell",
            hoverinfo="skip",
            showlegend=True,
        ),
        row=2, col=1,
    )

    # Theta threshold line
    if len(ts_dt) > 0:
        fig.add_trace(
            go.Scatter(
                x=[ts_dt[0], ts_dt[-1]],
                y=[_THETA, _THETA],
                mode="lines",
                line=dict(color="#FF6600", dash="dash", width=1.5),
                name=f"theta={_THETA:.2f}",
                hoverinfo="skip",
            ),
            row=2, col=1,
        )

    # EXIT_D fire markers on Panel 2 (at exit timestamp of each exit_d trade)
    if len(trades_df) > 0:
        exitd_mask = trades_df["exit_reason"] == "exit_d"
        if exitd_mask.any():
            ex_d_ts = trades_df.loc[exitd_mask, "exit_ts"].astype("int64").values
            ex_d_dt = pd.to_datetime(ex_d_ts, unit="ns", utc=True)
            # Find intensity_ratio values at those timestamps (nearest tick)
            idx_nearest = np.searchsorted(replay.timestamps_ns, ex_d_ts, side="left")
            idx_nearest = np.clip(idx_nearest, 0, len(replay.intensity_ratio) - 1)
            i_at_exit = replay.intensity_ratio[idx_nearest]

            fig.add_trace(
                go.Scatter(
                    x=ex_d_dt,
                    y=i_at_exit,
                    mode="markers",
                    marker=dict(
                        symbol="diamond", size=12, color="#FF6600",
                        line=dict(color="#552200", width=1),
                    ),
                    name="EXIT_D fire",
                    hovertemplate="EXIT_D fire<br>%{x|%H:%M:%S}<br>I(t)=%{y:.3f}<extra></extra>",
                ),
                row=2, col=1,
            )

    # ── Panel 3: Buy intensity I_buy(t) ──────────────────────────────────
    i_buy = 1.0 - replay.intensity_ratio  # NaN propagates correctly

    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=i_buy, mode="lines",
            line=dict(color="#9933CC", width=1),
            name="I_buy(t)",
            hoverinfo="skip",
            showlegend=True,
        ),
        row=3, col=1,
    )

    # (1 - theta) threshold line
    if len(ts_dt) > 0:
        fig.add_trace(
            go.Scatter(
                x=[ts_dt[0], ts_dt[-1]],
                y=[_BUY_THRESH, _BUY_THRESH],
                mode="lines",
                line=dict(color="#006600", dash="dash", width=1.5),
                name=f"re-entry thresh={_BUY_THRESH:.2f}",
                hoverinfo="skip",
            ),
            row=3, col=1,
        )

    # Re-entry markers on Panel 3 (at entry timestamp of each reentry trade)
    if len(trades_df) > 0:
        re_mask = trades_df["entry_type"] == "reentry"
        if re_mask.any():
            re_ent_ts = trades_df.loc[re_mask, "entry_ts"].astype("int64").values
            re_ent_dt = pd.to_datetime(re_ent_ts, unit="ns", utc=True)
            idx_nearest = np.searchsorted(replay.timestamps_ns, re_ent_ts, side="left")
            idx_nearest = np.clip(idx_nearest, 0, len(i_buy) - 1)
            ibuy_at_entry = i_buy[idx_nearest]

            fig.add_trace(
                go.Scatter(
                    x=re_ent_dt,
                    y=ibuy_at_entry,
                    mode="markers",
                    marker=dict(
                        symbol="diamond", size=12, color="#3399FF",
                        line=dict(color="#001155", width=1),
                    ),
                    name="Re-entry fire",
                    hovertemplate="Re-entry<br>%{x|%H:%M:%S}<br>I_buy=%{y:.3f}<extra></extra>",
                ),
                row=3, col=1,
            )

    # ── Layout ───────────────────────────────────────────────────────────
    fig.update_layout(
        title=title,
        width=1400,
        height=900,
        template="plotly_white",
        margin=dict(t=70, l=60, r=20, b=40),
        legend=dict(orientation="h", y=-0.04, x=0),
        xaxis_rangeslider_visible=False,
    )

    # Candlestick rangeslider off explicitly
    fig.update_layout(xaxis=dict(rangeslider=dict(visible=False)))

    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="I(t) sell", range=[0, 1], row=2, col=1)
    fig.update_yaxes(title_text="I_buy(t)", range=[0, 1], row=3, col=1)
    fig.update_yaxes(
        title_text="EPG state",
        range=[0, 1],
        showticklabels=False,
        row=4, col=1,
    )

    return fig
