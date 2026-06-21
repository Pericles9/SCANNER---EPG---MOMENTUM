"""Per-event 4-panel chart for EPG-Rapid R0 baseline.

Panel layout (shared x-axis, all at tick resolution except P3):
  P1: 10s OHLCV candlesticks + entry (▲ green) + exit (▼ green/red) + T_event vline
  P2: I(t) sell fraction = λ_sell / (λ_buy + λ_sell) per tick; θ=0.65 reference
  P3: q_tilde step function (1-min bars), 0.65 threshold dashed, entry-eligible shaded
  P4: EPG state colored band strip (INACTIVE/WARMUP/PASS/FAIL)

PASS windows are shaded green across P1–P3.

Public API:
    build_chart(ticker, date, timestamps_ns, prices, sf, bar_starts,
                lam_buy, lam_sell, epg_state_ints, t_event_ns,
                trade, n_hold) -> go.Figure
"""
from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_STATE_COLORS = {
    0: "#CCCCCC",  # INACTIVE
    1: "#FFEE66",  # WARMUP
    2: "#00CC44",  # PASS
    3: "#FF3333",  # FAIL
}
_PASS_FILL = "rgba(0,200,80,0.10)"
_ELIGIBLE_FILL = "rgba(0,180,255,0.15)"
_THETA = 0.65


def _build_ohlcv_10s(timestamps_ns: np.ndarray, prices: np.ndarray) -> pd.DataFrame:
    ts = pd.to_datetime(timestamps_ns, unit="ns", utc=True)
    s = pd.Series(prices.astype(float), index=ts)
    return s.resample("10s").ohlc().dropna()


def _state_runs(state_ints: np.ndarray, timestamps_ns: np.ndarray):
    if len(state_ints) == 0:
        return
    cur = int(state_ints[0])
    start = 0
    for i in range(1, len(state_ints)):
        if int(state_ints[i]) != cur:
            yield int(timestamps_ns[start]), int(timestamps_ns[i - 1]), cur
            cur = int(state_ints[i])
            start = i
    yield int(timestamps_ns[start]), int(timestamps_ns[-1]), cur


def _eligible_runs(mask: np.ndarray, bar_starts_ns: np.ndarray):
    """Yield (start_ns, end_ns) for consecutive True spans in mask."""
    if len(mask) == 0:
        return
    in_run = False
    run_start = 0
    bar_ns = 60 * 1_000_000_000
    for i in range(len(mask)):
        if mask[i] and not in_run:
            in_run = True
            run_start = i
        elif not mask[i] and in_run:
            in_run = False
            yield int(bar_starts_ns[run_start]), int(bar_starts_ns[i - 1]) + bar_ns
    if in_run:
        yield int(bar_starts_ns[run_start]), int(bar_starts_ns[-1]) + bar_ns


def _entry_eligible_mask(q_tilde: np.ndarray, n_hold: int) -> np.ndarray:
    n = len(q_tilde)
    mask = np.zeros(n, dtype=bool)
    for i in range(n_hold - 1, n):
        mask[i] = bool(np.all(q_tilde[i - n_hold + 1:i + 1] >= _THETA))
    return mask


def build_chart(
    ticker: str,
    date: str,
    timestamps_ns: np.ndarray,
    prices: np.ndarray,
    sf,
    bar_starts: np.ndarray,
    lam_buy: np.ndarray,
    lam_sell: np.ndarray,
    epg_state_ints: np.ndarray,
    t_event_ns: int | None,
    trade: dict | None,
    n_hold: int = 15,
) -> go.Figure:
    """Build 4-panel EPG-Rapid R0 chart for one event.

    Parameters
    ----------
    sf : SetupFilterResult
    bar_starts : int64 ns timestamps for each 1-min bar in sf.q_tilde
    epg_state_ints : int array (0=INACTIVE, 1=WARMUP, 2=PASS, 3=FAIL), len=len(timestamps_ns)
    trade : per-trade dict from per_trade.json, or None if no trade
    """
    ts_dt = pd.to_datetime(timestamps_ns, unit="ns", utc=True)
    ohlcv = _build_ohlcv_10s(timestamps_ns, prices)

    lambda_hat = lam_buy + lam_sell
    sell_frac = np.where(lambda_hat > 0, lam_sell / lambda_hat, 0.5)

    q_tilde = sf.q_tilde
    bar_dt = pd.to_datetime(bar_starts, unit="ns", utc=True)
    eligible_mask = _entry_eligible_mask(q_tilde, n_hold)

    n_pass_edges = int(np.sum(np.diff(np.where(epg_state_ints == 2, 1, 0).astype(int)) > 0))

    pnl_str = f"{trade['pnl_pct']:.2f}%" if trade else "no_trade"
    exit_str = trade.get("exit_reason", "") if trade else ""
    title = (
        f"{ticker} {date} | EPG-Rapid R0 | "
        f"n_hold={n_hold} | pnl={pnl_str} | {exit_str} | "
        f"pass_edges={n_pass_edges}"
    )

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        row_heights=[4, 2.5, 2.5, 1],
        vertical_spacing=0.03,
        subplot_titles=["Price + Entry/Exit", "I(t) sell", "q_tilde", "EPG state"],
    )

    # ── Anchor traces (keep x range stable) ──────────────────────────────
    anchor_x = ts_dt[:1] if len(ts_dt) else pd.to_datetime([], utc=True)
    for row in [1, 2, 3, 4]:
        fig.add_trace(
            go.Scatter(x=anchor_x, y=[0.5] if len(anchor_x) else [],
                       mode="markers", marker=dict(opacity=0),
                       showlegend=False, hoverinfo="skip"),
            row=row, col=1,
        )

    # ── PASS window vrects across P1–P3 ──────────────────────────────────
    for start_ns, end_ns, state_int in _state_runs(epg_state_ints, timestamps_ns):
        if state_int == 2:
            x0 = pd.Timestamp(start_ns, unit="ns", tz="UTC")
            x1 = pd.Timestamp(end_ns, unit="ns", tz="UTC")
            for row in [1, 2, 3]:
                fig.add_vrect(x0=x0, x1=x1, fillcolor=_PASS_FILL,
                              line_width=0, layer="below", row=row, col=1)

    # ── Entry-eligible vrects in P3 ────────────────────────────────────
    if len(bar_starts) > 0:
        for start_ns, end_ns in _eligible_runs(eligible_mask, bar_starts):
            x0 = pd.Timestamp(start_ns, unit="ns", tz="UTC")
            x1 = pd.Timestamp(end_ns, unit="ns", tz="UTC")
            fig.add_vrect(x0=x0, x1=x1, fillcolor=_ELIGIBLE_FILL,
                          line_width=0, layer="below", row=3, col=1)

    # ── T_event vertical marker ─────────────────────────────────────────
    if t_event_ns is not None:
        t_ev_dt = pd.Timestamp(int(t_event_ns), unit="ns", tz="UTC")
        for row in [1, 2, 3, 4]:
            fig.add_vline(x=t_ev_dt, line=dict(color="#FFA500", width=1, dash="dot"),
                          row=row, col=1)

    # ── P4: EPG state colored band strip ──────────────────────────────────
    for start_ns, end_ns, state_int in _state_runs(epg_state_ints, timestamps_ns):
        x0 = pd.Timestamp(start_ns, unit="ns", tz="UTC")
        x1 = pd.Timestamp(end_ns, unit="ns", tz="UTC")
        color_hex = _STATE_COLORS.get(state_int, "#CCCCCC")
        r_v, g_v, b_v = (int(color_hex[1:3], 16), int(color_hex[3:5], 16), int(color_hex[5:7], 16))
        fig.add_vrect(x0=x0, x1=x1,
                      fillcolor=f"rgba({r_v},{g_v},{b_v},0.45)",
                      line_width=0, layer="below", row=4, col=1)

    # ── P1: OHLCV + entry/exit ────────────────────────────────────────────
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

    if trade is not None:
        entry_ts = int(trade["entry_ts"])
        exit_ts = int(trade["exit_ts"])
        entry_price = float(trade["entry_price"])
        exit_price = float(trade["exit_price"])
        pnl_pct = float(trade["pnl_pct"])

        fig.add_trace(
            go.Scatter(
                x=[pd.Timestamp(entry_ts, unit="ns", tz="UTC")],
                y=[entry_price],
                mode="markers",
                marker=dict(symbol="triangle-up", size=14, color="#00CC44",
                            line=dict(color="#003311", width=1)),
                name="Entry",
                hovertemplate=f"Entry<br>%{{x|%H:%M:%S}}<br>price %{{y:.4f}}<extra></extra>",
            ),
            row=1, col=1,
        )

        exit_color = "#00CC44" if pnl_pct > 0 else ("#CC2200" if pnl_pct < 0 else "#888888")
        fig.add_trace(
            go.Scatter(
                x=[pd.Timestamp(exit_ts, unit="ns", tz="UTC")],
                y=[exit_price],
                mode="markers",
                marker=dict(symbol="triangle-down", size=14, color=exit_color,
                            line=dict(color="#222222", width=1)),
                name="Exit",
                hovertemplate=(
                    f"Exit ({exit_str})<br>%{{x|%H:%M:%S}}<br>"
                    f"price %{{y:.4f}}<br>pnl {pnl_pct:.3f}%<extra></extra>"
                ),
            ),
            row=1, col=1,
        )

    # ── P2: I(t) sell ────────────────────────────────────────────────────
    fig.add_trace(
        go.Scatter(
            x=ts_dt, y=sell_frac,
            mode="lines",
            line=dict(color="#CC4400", width=1),
            name="I(t) sell",
            hovertemplate="I(t)=%{y:.3f}<br>%{x|%H:%M:%S}<extra></extra>",
        ),
        row=2, col=1,
    )
    # θ reference line
    fig.add_hline(y=_THETA, line=dict(color="#888888", width=1, dash="dash"),
                  row=2, col=1)

    # ── P3: q_tilde ──────────────────────────────────────────────────────
    if len(q_tilde) > 0 and len(bar_starts) > 0:
        # Step function: extend each bar to the next bar start
        bar_ns = 60 * 1_000_000_000
        next_starts = np.concatenate([bar_starts[1:], [bar_starts[-1] + bar_ns]])
        x_step, y_step = [], []
        for i in range(len(q_tilde)):
            x_step.append(pd.Timestamp(int(bar_starts[i]), unit="ns", tz="UTC"))
            y_step.append(float(q_tilde[i]))
            x_step.append(pd.Timestamp(int(next_starts[i]), unit="ns", tz="UTC"))
            y_step.append(float(q_tilde[i]))

        fig.add_trace(
            go.Scatter(
                x=x_step, y=y_step,
                mode="lines",
                line=dict(color="#0055AA", width=1.5),
                name="q̃",
                hovertemplate="q̃=%{y:.3f}<br>%{x|%H:%M:%S}<extra></extra>",
            ),
            row=3, col=1,
        )

    # Threshold line
    fig.add_hline(y=_THETA, line=dict(color="#888888", width=1, dash="dash"),
                  row=3, col=1)

    fig.update_layout(
        title=title,
        height=900,
        showlegend=True,
        xaxis_rangeslider_visible=False,
        xaxis2_rangeslider_visible=False,
        xaxis3_rangeslider_visible=False,
        xaxis4_rangeslider_visible=False,
        margin=dict(l=60, r=40, t=60, b=40),
        template="plotly_dark",
    )
    # Fix y-axis ranges for stable panels
    fig.update_yaxes(range=[0, 1], row=2, col=1, title_text="I(t) sell")
    fig.update_yaxes(range=[-0.05, 1.05], row=3, col=1, title_text="q̃")
    fig.update_yaxes(row=4, col=1, showticklabels=False)

    return fig
