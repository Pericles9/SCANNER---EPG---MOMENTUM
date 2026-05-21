#!/usr/bin/env python3
"""Phase A — Per-event signal chart builder.

`make_event_chart()` writes a single standalone Plotly HTML file with
five stacked panels:
    1. 1-minute OHLCV candlesticks + EPG PASS shading + T_event marker + prev_close line
    2. Tick price + L1 bid/ask + LULD lower band + EPG PASS shading + entry/exit triangles
    3. lambda_V intensity + running_peak + threshold + T_event marker
    4. EPG gate state step function + PASS / WARMUP shading
    5. LULD proximity state step trace + EXIT_HALT shading

Public API: `make_event_chart(ticker, date, trades, epg_data, prev_close, output_path,
                               luld_data=None)`.
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data.loaders.trades import load_trades, list_events, load_bars_1m
from data.loaders.quotes import load_quotes


# State numeric encoding for Panel 4
_STATE_NUM = {
    "INACTIVE": 0,
    "WARMUP": 1,
    "PASS": 2,
    "FAIL": 3,
}


def _resolve_mom_pct(ticker: str, date: str) -> float:
    """Look up mom_pct for an event from the catalog."""
    for ev in list_events(min_mom=0.0, require_date=True):
        if ev["ticker"] == ticker and ev["date"] == date:
            return ev["mom_pct"]
    raise FileNotFoundError(f"No event in catalog for {ticker} {date}")


def _add_pass_shading(fig: go.Figure, pass_windows: list[dict],
                      row: int, fillcolor: str = "rgba(0,200,80,0.12)") -> None:
    """Add light green vrects for each PASS window on the given subplot row.

    `add_vrect` with row/col is the only Plotly API that pins a shape to one
    panel of a multi-row subplot.
    """
    for w in pass_windows:
        x0 = pd.Timestamp(w["open_ts"], unit="ns", tz="UTC")
        x1 = pd.Timestamp(w["close_ts"], unit="ns", tz="UTC")
        fig.add_vrect(
            x0=x0, x1=x1,
            fillcolor=fillcolor, line_width=0, layer="below",
            row=row, col=1,
        )


def _add_warmup_shading(fig: go.Figure, epg_timeline: list[dict],
                        row: int) -> None:
    """Add light yellow vrects for contiguous WARMUP runs on the given row."""
    if not epg_timeline:
        return
    open_ts = None
    for entry in epg_timeline:
        if entry["state"] == "WARMUP" and open_ts is None:
            open_ts = entry["ts"]
        elif entry["state"] != "WARMUP" and open_ts is not None:
            x0 = pd.Timestamp(open_ts, unit="ns", tz="UTC")
            x1 = pd.Timestamp(entry["ts"], unit="ns", tz="UTC")
            fig.add_vrect(
                x0=x0, x1=x1,
                fillcolor="rgba(255,220,80,0.15)", line_width=0,
                layer="below", row=row, col=1,
            )
            open_ts = None
    if open_ts is not None:
        x0 = pd.Timestamp(open_ts, unit="ns", tz="UTC")
        x1 = pd.Timestamp(epg_timeline[-1]["ts"], unit="ns", tz="UTC")
        fig.add_vrect(
            x0=x0, x1=x1,
            fillcolor="rgba(255,220,80,0.15)", line_width=0,
            layer="below", row=row, col=1,
        )


def _event_pf(trades: pd.DataFrame) -> float:
    """Profit factor for one event. Returns 1.0 if no losses (or no trades)."""
    if trades.empty:
        return 1.0
    pnl = trades["pnl_pct"].values
    win_sum = float(pnl[pnl > 0].sum())
    loss_sum = float(np.abs(pnl[pnl < 0].sum()))
    if loss_sum < 1e-12:
        return 1.0 if win_sum == 0 else float("inf")
    return win_sum / loss_sum


def make_event_chart(
    ticker: str,
    date: str,
    trades: pd.DataFrame,
    epg_data: dict,
    prev_close: float,
    output_path: str,
    luld_data: list | None = None,
) -> None:
    """Render a single Phase A signal chart for one event.

    `trades` must contain at least: entry_ts, exit_ts, entry_price, exit_price,
    pnl_pct, exit_reason.

    `epg_data` is the dict returned by `replay_epg_for_event`.
    `luld_data` is the `luld_timeline` list from `replay_epg_for_event`
    (list of {ts, state, lower_band} per tick). If None, LULD panels are skipped.
    """
    mom_pct = _resolve_mom_pct(ticker, date)
    td = load_trades(ticker, date, mom_pct)
    qd = load_quotes(ticker, date, mom_pct)
    bars_1m = load_bars_1m(ticker, date, mom_pct)

    pass_windows = epg_data.get("pass_windows", [])
    epg_timeline = epg_data.get("epg_timeline", [])
    t_event_ns = epg_data.get("t_event")
    t_event_ts = (pd.Timestamp(t_event_ns, unit="ns", tz="UTC")
                  if t_event_ns is not None else None)

    # ── Build subplots ──
    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True,
        row_heights=[3, 3, 2, 1, 1],
        vertical_spacing=0.03,
        subplot_titles=("1-Minute OHLCV", "Tick Price + L1",
                        "λ_V Intensity", "EPG Gate State",
                        "LULD Proximity State"),
    )

    # ─────────── Panel 1: 1-min candles ───────────
    if not bars_1m.empty:
        fig.add_trace(
            go.Candlestick(
                x=bars_1m["ts"],
                open=bars_1m["open"], high=bars_1m["high"],
                low=bars_1m["low"], close=bars_1m["close"],
                increasing_line_color="#26a69a",
                decreasing_line_color="#ef5350",
                showlegend=False, name="1m",
            ),
            row=1, col=1,
        )
    # PASS shading on Panel 1
    _add_pass_shading(fig, pass_windows, row=1)

    # prev_close horizontal line on Panel 1
    if prev_close and prev_close > 0 and not bars_1m.empty:
        x0 = bars_1m["ts"].iloc[0]
        x1 = bars_1m["ts"].iloc[-1]
        fig.add_trace(
            go.Scatter(
                x=[x0, x1], y=[prev_close, prev_close],
                mode="lines",
                line=dict(color="#888888", width=1, dash="dash"),
                name=f"prev_close {prev_close:.4f}",
                hoverinfo="skip", showlegend=True,
            ),
            row=1, col=1,
        )

    # ─────────── Panel 2: tick price + L1 ───────────
    tick_ts = pd.to_datetime(td.timestamps, unit="ns", utc=True)
    fig.add_trace(
        go.Scatter(
            x=tick_ts, y=td.prices, mode="lines",
            line=dict(color="#AAAAAA", width=1),
            name="trade price", hoverinfo="skip",
        ),
        row=2, col=1,
    )

    quote_ts = pd.to_datetime(qd.timestamps, unit="ns", utc=True)
    fig.add_trace(
        go.Scatter(
            x=quote_ts, y=qd.bid_prices, mode="lines",
            line=dict(color="#FF6666", width=1),
            opacity=0.5, name="bid", hoverinfo="skip",
        ),
        row=2, col=1,
    )
    fig.add_trace(
        go.Scatter(
            x=quote_ts, y=qd.ask_prices, mode="lines",
            line=dict(color="#6699FF", width=1),
            opacity=0.5, name="ask", hoverinfo="skip",
        ),
        row=2, col=1,
    )

    # PASS shading on Panel 2
    _add_pass_shading(fig, pass_windows, row=2)

    # LULD lower band on Panel 2
    if luld_data:
        lb_ts = [r["ts"] for r in luld_data if r["lower_band"] is not None]
        lb_vals = [r["lower_band"] for r in luld_data if r["lower_band"] is not None]
        if lb_ts:
            fig.add_trace(
                go.Scatter(
                    x=pd.to_datetime(lb_ts, unit="ns", utc=True),
                    y=lb_vals,
                    mode="lines",
                    line=dict(color="#FF4444", width=1, dash="dash"),
                    name="LULD lower band",
                    hoverinfo="skip",
                ),
                row=2, col=1,
            )

    # Entry / exit markers — colored by exit reason
    if not trades.empty:
        ent_ts = pd.to_datetime(trades["entry_ts"].astype("int64").values,
                                unit="ns", utc=True)
        ex_ts = pd.to_datetime(trades["exit_ts"].astype("int64").values,
                               unit="ns", utc=True)
        ent_p = trades["entry_price"].values
        ex_p = trades["exit_price"].values
        pnl = trades["pnl_pct"].values
        reasons = (trades["exit_reason"].values
                   if "exit_reason" in trades.columns
                   else [""] * len(pnl))

        fig.add_trace(
            go.Scatter(
                x=ent_ts, y=ent_p, mode="markers",
                marker=dict(symbol="triangle-up", size=10, color="#00CC44",
                            line=dict(color="#005522", width=1)),
                name="entry",
                hovertemplate="entry %{x|%H:%M:%S}<br>price %{y:.4f}<extra></extra>",
            ),
            row=2, col=1,
        )
        _EXIT_COLORS = {
            "luld_proximity": "#FF3333",      # red fill
            "epg_window_close": "#FFFFFF",    # white fill
            "session_end": "#AAAAAA",         # gray
        }
        exit_colors = [_EXIT_COLORS.get(r, "#AAAAAA") for r in reasons]
        exit_line_colors = ["#CC0000" if r == "luld_proximity" else "#444444"
                            for r in reasons]
        fig.add_trace(
            go.Scatter(
                x=ex_ts, y=ex_p, mode="markers",
                marker=dict(symbol="triangle-down", size=10, color=exit_colors,
                            line=dict(color=exit_line_colors, width=1.5)),
                name="exit",
                customdata=np.column_stack([pnl, reasons]),
                hovertemplate=("exit %{x|%H:%M:%S}<br>price %{y:.4f}"
                               "<br>pnl %{customdata[0]:.3f}%"
                               "<br>reason: %{customdata[1]}<extra></extra>"),
            ),
            row=2, col=1,
        )

    # ─────────── Panel 3: lambda_V ───────────
    if epg_timeline:
        ts_arr = pd.to_datetime([r["ts"] for r in epg_timeline],
                                unit="ns", utc=True)
        lam_v = [r["lambda_v"] for r in epg_timeline]
        peak = [r["running_peak"] for r in epg_timeline]
        thresh = [r["threshold"] for r in epg_timeline]

        fig.add_trace(
            go.Scatter(x=ts_arr, y=lam_v, mode="lines",
                       line=dict(color="#4488FF", width=1),
                       name="λ_V", hoverinfo="skip"),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(x=ts_arr, y=peak, mode="lines",
                       line=dict(color="#AAAAFF", width=1, dash="dash"),
                       name="running_peak", hoverinfo="skip"),
            row=3, col=1,
        )
        fig.add_trace(
            go.Scatter(x=ts_arr, y=thresh, mode="lines",
                       line=dict(color="#FF8844", width=1, dash="dash"),
                       name="threshold (p×peak)", hoverinfo="skip"),
            row=3, col=1,
        )

    # ─────────── Panel 4: EPG state step ───────────
    if epg_timeline:
        ts_arr = pd.to_datetime([r["ts"] for r in epg_timeline],
                                unit="ns", utc=True)
        state_num = [_STATE_NUM.get(r["state"], 0) for r in epg_timeline]
        fig.add_trace(
            go.Scatter(
                x=ts_arr, y=state_num, mode="lines",
                line=dict(color="#AAAAAA", width=1, shape="hv"),
                name="state", hoverinfo="skip", showlegend=False,
            ),
            row=4, col=1,
        )
    # PASS shading (heavier opacity on this row)
    _add_pass_shading(fig, pass_windows, row=4,
                      fillcolor="rgba(0,200,80,0.20)")
    # WARMUP shading
    _add_warmup_shading(fig, epg_timeline, row=4)

    # ─────────── Panel 5: LULD proximity state ───────────
    if luld_data:
        luld_ts = pd.to_datetime([r["ts"] for r in luld_data], unit="ns", utc=True)
        luld_state_nums = [r["state"] for r in luld_data]
        fig.add_trace(
            go.Scatter(
                x=luld_ts, y=luld_state_nums, mode="lines",
                line=dict(color="#FF6666", width=1, shape="hv"),
                name="LULD state", hoverinfo="skip", showlegend=False,
            ),
            row=5, col=1,
        )
        # Light red shading for EXIT_HALT regions
        in_halt = False
        halt_open_ts = None
        for r in luld_data:
            if r["state"] == 2 and not in_halt:
                in_halt = True
                halt_open_ts = r["ts"]
            elif r["state"] != 2 and in_halt:
                x0 = pd.Timestamp(halt_open_ts, unit="ns", tz="UTC")
                x1 = pd.Timestamp(r["ts"], unit="ns", tz="UTC")
                fig.add_vrect(
                    x0=x0, x1=x1,
                    fillcolor="rgba(255,80,80,0.25)", line_width=0,
                    layer="below", row=5, col=1,
                )
                in_halt = False
        if in_halt and halt_open_ts is not None:
            x0 = pd.Timestamp(halt_open_ts, unit="ns", tz="UTC")
            x1 = pd.Timestamp(luld_data[-1]["ts"], unit="ns", tz="UTC")
            fig.add_vrect(
                x0=x0, x1=x1,
                fillcolor="rgba(255,80,80,0.25)", line_width=0,
                layer="below", row=5, col=1,
            )

    # ─────────── T_event vline (all panels) ───────────
    if t_event_ts is not None:
        for r in (1, 2, 3, 4, 5):
            fig.add_vline(
                x=t_event_ts, line=dict(color="#FFA500", width=1, dash="dash"),
                row=r, col=1,
            )

    # ─────────── Layout / axes ───────────
    n_trades = len(trades)
    pf = _event_pf(trades)
    if not trades.empty:
        first = trades.sort_values("entry_ts").iloc[0]
        gap_pct = (first["entry_price"] - prev_close) / prev_close * 100.0
    else:
        gap_pct = float("nan")

    title = (f"{ticker} {date} | PF={pf:.4f} | {n_trades} trades | "
             f"gap={gap_pct:.1f}%")

    fig.update_layout(
        title=title,
        height=1400,
        template="plotly_white",
        xaxis_rangeslider_visible=False,  # candle row default — hide
        hovermode="x unified",
        showlegend=True,
        legend=dict(orientation="h", yanchor="bottom", y=1.02,
                    xanchor="right", x=1),
    )

    # Disable rangeslider on the candle row
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)

    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="Price (tick)", row=2, col=1)
    fig.update_yaxes(title_text="λ_V", row=3, col=1)
    fig.update_yaxes(
        title_text="EPG state",
        range=[-0.5, 3.5],
        tickvals=[0, 1, 2, 3],
        ticktext=["INACTIVE", "WARMUP", "PASS", "FAIL"],
        row=4, col=1,
    )
    fig.update_yaxes(
        title_text="LULD state",
        range=[-0.5, 2.5],
        tickvals=[0, 1, 2],
        ticktext=["INACTIVE", "ARMED", "EXIT_HALT"],
        row=5, col=1,
    )
    # Bottom axis label
    fig.update_xaxes(title_text="Time (UTC)", row=5, col=1)

    # ─────────── Top-right annotations on Panel 2 ───────────
    if not trades.empty:
        first_session = trades.iloc[0].get("session_bucket", "")
    else:
        first_session = ""
    t_event_str = (t_event_ts.tz_convert("America/New_York").strftime("%H:%M:%S ET")
                   if t_event_ts is not None else "n/a")
    ann_text = (f"Session: {first_session}<br>"
                f"T_event: {t_event_str}<br>"
                f"prev_close: {prev_close:.4f}")
    fig.add_annotation(
        text=ann_text,
        xref="paper", yref="paper",
        x=0.99, y=0.74,  # top-right of Panel 2 (rows 1+2 split ~half the figure)
        xanchor="right", yanchor="top",
        showarrow=False,
        bgcolor="rgba(255,255,255,0.85)",
        bordercolor="#888", borderwidth=1,
        font=dict(size=11, family="monospace"),
    )

    fig.write_html(output_path, include_plotlyjs="cdn")
