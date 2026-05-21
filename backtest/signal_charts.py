#!/usr/bin/env python3
"""
Phase E — Interactive Signal Diagnostic Charts (Plotly)
========================================================
Multi-panel interactive charts: candlesticks + all signal time series + thresholds.

For each selected event:
  Panel 1: 10s candlestick chart with entry/exit markers + spread band
  Panel 2: lambda_hat (Hawkes intensity) with T_event threshold
  Panel 3: E(t) + E_min threshold; Edot(t) on secondary y-axis + theta_slope
  Panel 4: lambda_V (EPG) + peak + threshold + state coloring
  Panel 5: delta_mid_5s + Gate 3 threshold (±K*spread)
  Panel 6: vol_accel + zero line

Reads from: results/phase_e/backtest_val_trades.json
Writes to:  results/phase_e/signal_charts/ (interactive .html files)
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.hawkes.engine import hawkes_replay_fixed_beta
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.features.volume_acceleration import compute_vol_accel_series

# ── Constants ──
LN2 = math.log(2)
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0

E_MIN = 1.5
THETA_SLOPE = 5.0
GATE3_K = 0.25

CANDLE_BAR_SEC = 10
CHART_DIR = Path(__file__).resolve().parent.parent / "results" / "phase_e_v6" / "signal_charts"


# ══════════════════════════════════════════════════════════════════════
#  Candle builder
# ══════════════════════════════════════════════════════════════════════

def build_candles(t_sec, prices, sizes, bar_sec=CANDLE_BAR_SEC):
    """Build OHLCV arrays from raw trades. Returns (bar_center, O, H, L, C, V)."""
    if len(t_sec) == 0:
        empty = np.array([], dtype=np.float64)
        return empty, empty, empty, empty, empty, empty

    t_min, t_max = t_sec[0], t_sec[-1]
    bar_start = int(t_min / bar_sec) * bar_sec
    n_bars = int((t_max - bar_start) / bar_sec) + 1

    opens = np.full(n_bars, np.nan)
    highs = np.full(n_bars, np.nan)
    lows = np.full(n_bars, np.nan)
    closes = np.full(n_bars, np.nan)
    volumes = np.zeros(n_bars)
    bar_times = np.arange(n_bars) * bar_sec + bar_start + bar_sec / 2

    for i in range(len(t_sec)):
        idx = min(int((t_sec[i] - bar_start) / bar_sec), n_bars - 1)
        p = prices[i]
        if np.isnan(opens[idx]):
            opens[idx] = p
            highs[idx] = p
            lows[idx] = p
        else:
            highs[idx] = max(highs[idx], p)
            lows[idx] = min(lows[idx], p)
        closes[idx] = p
        volumes[idx] += sizes[i]

    return bar_times, opens, highs, lows, closes, volumes


# ══════════════════════════════════════════════════════════════════════
#  Signal replay (same logic as backtest runner, but collect everything)
# ══════════════════════════════════════════════════════════════════════

def replay_all_signals(ticker, date, mom_pct, hawkes_params, rho_E, q_bar_cfg):
    """Replay all signals for one event. Returns dict of time series."""
    td = load_trades(ticker, date, mom_pct)
    qd = load_quotes(ticker, date, mom_pct)
    N = td.n_trades

    # Sides
    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
    ofi_result = compute_trade_ofi(
        trade_timestamps=td.timestamps,
        trade_prices=td.prices,
        trade_sizes=td.sizes.astype(np.float64),
        quote_timestamps=qd.timestamps,
        quote_bid_prices=qd.bid_prices,
        quote_ask_prices=qd.ask_prices,
        quote_bid_sizes=qd.bid_sizes.astype(np.float64),
        quote_ask_sizes=qd.ask_sizes.astype(np.float64),
        window_sec=10.0,
        q_bar_fallback=tier_qbar,
    )
    sides = ofi_result.sides

    # Hawkes
    fp = hawkes_params
    lam_buy = np.zeros(N, dtype=np.float64)
    lam_sell = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)

    hawkes_replay_fixed_beta(
        t_sec=td.t_sec, sides=sides,
        alpha_self_buy=fp["alpha_buy_self"],
        alpha_cross_buy=fp.get("alpha_buy_cross", 0.0),
        alpha_self_sell=fp["alpha_sell_self"],
        alpha_cross_sell=fp.get("alpha_sell_cross", 0.0),
        mu_buy=fp["mu_buy"], mu_sell=fp["mu_sell"],
        beta_mle=fp["beta"], rho_E=rho_E,
        lam_buy_out=lam_buy, lam_sell_out=lam_sell,
        E_out=E_out, Edot_out=Edot_out,
    )
    lambda_hat = lam_buy + lam_sell
    lambda_ref = fp["mu_buy"] + fp["mu_sell"]

    # EPG
    anchor = EventAnchor(lambda_ref=lambda_ref, k_multiplier=EPG_K)
    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P,
        warmup_seconds=EPG_WARMUP,
    )

    epg_states = []
    lambda_v = np.zeros(N, dtype=np.float64)
    lambda_v_peak = np.zeros(N, dtype=np.float64)
    lambda_v_thresh = np.zeros(N, dtype=np.float64)
    t_event_sec = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and t_event_sec is None:
            gate.activate(t_ev)
            t_event_sec = t_ev

        dv = float(td.prices[i]) * float(td.sizes[i])
        gs = gate.update(dv, td.t_sec[i])
        epg_states.append(gs)
        lambda_v[i] = gate.lambda_v
        lambda_v_peak[i] = gate.lambda_v_peak
        lambda_v_thresh[i] = gate.threshold

    # Mid + spread at each trade
    mid = np.empty(N, dtype=np.float64)
    spread = np.empty(N, dtype=np.float64)
    q_idx = 0
    for i in range(N):
        while q_idx < qd.n_quotes - 1 and qd.timestamps[q_idx + 1] <= td.timestamps[i]:
            q_idx += 1
        mid[i] = (qd.bid_prices[q_idx] + qd.ask_prices[q_idx]) / 2.0
        spread[i] = qd.ask_prices[q_idx] - qd.bid_prices[q_idx]

    # Delta mid 5s
    dm5 = np.full(N, np.nan, dtype=np.float64)
    j = 0
    for i in range(N):
        target = td.t_sec[i] - 5.0
        if target < td.t_sec[0]:
            continue
        while j < i - 1 and td.t_sec[j + 1] <= target:
            j += 1
        dm5[i] = mid[i] - mid[j]

    # Vol accel
    va = compute_vol_accel_series(td.timestamps, td.sizes, window_sec=5, min_trades=10)

    return {
        "td": td, "qd": qd, "sides": sides,
        "lambda_hat": lambda_hat, "lambda_ref": lambda_ref,
        "E": E_out, "Edot": Edot_out,
        "epg_states": epg_states,
        "lambda_v": lambda_v, "lambda_v_peak": lambda_v_peak,
        "lambda_v_thresh": lambda_v_thresh,
        "t_event": t_event_sec,
        "mid": mid, "spread": spread,
        "dm5": dm5, "vol_accel": va,
        "gate3_thresh": GATE3_K * spread,
    }


# ══════════════════════════════════════════════════════════════════════
#  Plotly chart generator
# ══════════════════════════════════════════════════════════════════════

def _epg_state_vrects(t, epg_states, x_min, x_max):
    """Build list of (x0, x1, color) for EPG state background bands."""
    COL = {
        GateState.WARMUP: "rgba(255,167,38,0.15)",   # orange
        GateState.PASS: "rgba(76,175,80,0.18)",       # green
        GateState.FAIL: "rgba(244,67,54,0.12)",       # red
    }
    rects = []
    prev_state = None
    block_start = None
    for i in range(len(t)):
        if t[i] < x_min or t[i] > x_max:
            continue
        st = epg_states[i]
        if st != prev_state:
            if prev_state in COL and block_start is not None:
                rects.append((block_start, t[i], COL[prev_state]))
            block_start = t[i]
            prev_state = st
    # close last
    if prev_state in COL and block_start is not None:
        rects.append((block_start, min(t[-1], x_max), COL[prev_state]))
    return rects


def generate_chart(signals, event_trades, output_path):
    """Generate interactive Plotly multi-panel chart showing ALL trades for an event."""
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    td = signals["td"]
    t = td.t_sec
    N = td.n_trades

    t_event = signals["t_event"]

    # Zoom range: T_event - 30s to last exit + 120s
    last_exit_t = max((tr.get("exit_t_sec", 0) for tr in event_trades), default=0)
    first_entry_t = min((tr.get("entry_t_sec", 0) for tr in event_trades), default=0)
    if first_entry_t is not None and t_event is not None:
        x_min = max(0, t_event - 30)
        x_max = min(last_exit_t + 120, t[-1])
        if x_max - x_min < 180:
            x_max = min(x_min + 180, t[-1])
    elif t_event is not None:
        x_min = max(0, t_event - 30)
        x_max = min(t_event + 600, t[-1])
    else:
        x_min, x_max = 0, min(600, t[-1])

    vis = (t >= x_min) & (t <= x_max)

    # Downsample if too many points for plotly performance
    MAX_PTS = 8000
    if vis.sum() > MAX_PTS:
        step = vis.sum() // MAX_PTS
        indices = np.where(vis)[0][::step]
        ds = np.zeros(N, dtype=bool)
        ds[indices] = True
        vis_ds = ds
    else:
        vis_ds = vis

    # ── Build figure: 7 rows (overview + 6 signal panels) ──
    # Row 1 = full-session 1m candle overview (independent x-axis)
    # Rows 2-7 = zoomed signal panels (shared x-axis among themselves)
    fig = make_subplots(
        rows=7, cols=1, shared_xaxes=False,
        vertical_spacing=0.025,
        row_heights=[0.16, 0.20, 0.12, 0.12, 0.12, 0.14, 0.14],
        subplot_titles=[
            "Full Session Overview (1m candles)",
            "Price (10s candles) + Bid-Ask Spread",
            "Hawkes Intensity lambda_hat(t)",
            "Excitation Ratio E(t) & Edot(t)",
            f"EPG Dollar Volume Intensity (tau={EPG_TAU:.0f}s)",
            "Trailing 5s Mid Change + Gate 3 Threshold",
            "Volume Acceleration (5s window)",
        ],
        specs=[[{"secondary_y": False}],
               [{"secondary_y": False}],
               [{"secondary_y": False}],
               [{"secondary_y": True}],
               [{"secondary_y": False}],
               [{"secondary_y": False}],
               [{"secondary_y": False}]],
    )

    # Link x-axes for rows 2-7 (zoomed panels share zoom)
    for row_num in range(3, 8):
        fig.update_xaxes(matches="x2", row=row_num, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 1: FULL SESSION overview — 1m candles, all data
    # ═══════════════════════════════════════════════════════════════
    OVERVIEW_BAR_SEC = 60
    all_bt, all_op, all_hi, all_lo, all_cl, all_vol = build_candles(
        t, td.prices, td.sizes, OVERVIEW_BAR_SEC,
    )
    all_valid = ~np.isnan(all_op)

    fig.add_trace(go.Candlestick(
        x=all_bt[all_valid],
        open=all_op[all_valid],
        high=all_hi[all_valid],
        low=all_lo[all_valid],
        close=all_cl[all_valid],
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
        name="Price (1m)", showlegend=False,
    ), row=1, col=1)

    # All entry/exit markers on overview — green=win, red=loss
    win_entries_t, win_entries_p = [], []
    loss_entries_t, loss_entries_p = [], []
    win_exits_t, win_exits_p = [], []
    loss_exits_t, loss_exits_p = [], []
    for tr in event_trades:
        et = tr.get("entry_t_sec")
        xt = tr.get("exit_t_sec")
        ep = tr.get("entry_price", 0)
        xp = tr.get("exit_price", 0)
        is_win = tr.get("pnl_pct", 0) > 0
        if et is not None:
            (win_entries_t if is_win else loss_entries_t).append(et)
            (win_entries_p if is_win else loss_entries_p).append(ep)
        if xt is not None:
            (win_exits_t if is_win else loss_exits_t).append(xt)
            (win_exits_p if is_win else loss_exits_p).append(xp)

    if win_entries_t:
        fig.add_trace(go.Scatter(
            x=win_entries_t, y=win_entries_p,
            mode="markers", marker=dict(symbol="triangle-up", size=8,
                                        color="#2e7d32", line=dict(width=0.5, color="black")),
            name="Entry (win)", showlegend=False,
        ), row=1, col=1)
    if loss_entries_t:
        fig.add_trace(go.Scatter(
            x=loss_entries_t, y=loss_entries_p,
            mode="markers", marker=dict(symbol="triangle-up", size=8,
                                        color="#c62828", line=dict(width=0.5, color="black")),
            name="Entry (loss)", showlegend=False,
        ), row=1, col=1)
    if win_exits_t:
        fig.add_trace(go.Scatter(
            x=win_exits_t, y=win_exits_p,
            mode="markers", marker=dict(symbol="triangle-down", size=8,
                                        color="#2e7d32", line=dict(width=0.5, color="black")),
            name="Exit (win)", showlegend=False,
        ), row=1, col=1)
    if loss_exits_t:
        fig.add_trace(go.Scatter(
            x=loss_exits_t, y=loss_exits_p,
            mode="markers", marker=dict(symbol="triangle-down", size=8,
                                        color="#c62828", line=dict(width=0.5, color="black")),
            name="Exit (loss)", showlegend=False,
        ), row=1, col=1)

    # Vertical lines on overview
    if t_event is not None:
        fig.add_vline(x=t_event, line_color="purple", line_dash="dash",
                      line_width=1, opacity=0.5, row=1, col=1,
                      annotation_text="T_event", annotation_position="top")

    # Shade the zoomed region on the overview
    fig.add_vrect(x0=x_min, x1=x_max, fillcolor="rgba(33,150,243,0.08)",
                  line=dict(color="rgba(33,150,243,0.4)", width=1, dash="dot"),
                  layer="below", row=1, col=1)

    fig.update_xaxes(range=[0, t[-1]], row=1, col=1)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)

    # ── EPG state background rects (on zoomed rows 2-7) ──
    vrects = _epg_state_vrects(t, signals["epg_states"], x_min, x_max)
    for x0, x1, col in vrects:
        for row in range(2, 8):
            fig.add_vrect(x0=x0, x1=x1, fillcolor=col,
                          layer="below", line_width=0, row=row, col=1)

    # ── Vertical lines: T_event + all entry/exit pairs (zoomed rows 2-7) ──
    if t_event is not None:
        for row in range(2, 8):
            fig.add_vline(
                x=t_event, line_color="purple", line_dash="dash",
                line_width=1.5, opacity=0.7, row=row, col=1,
                annotation_text="T_event" if row == 2 else None,
                annotation_position="top" if row == 2 else None,
            )

    # Position-held shading on price row (row 2): green=win, red=loss
    for tr in event_trades:
        et = tr.get("entry_t_sec")
        xt = tr.get("exit_t_sec")
        if et is not None and xt is not None:
            is_win = tr.get("pnl_pct", 0) > 0
            fill = "rgba(76,175,80,0.15)" if is_win else "rgba(244,67,54,0.15)"
            fig.add_vrect(x0=et, x1=xt, fillcolor=fill,
                          line_width=0, layer="below", row=2, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 2: Zoomed Candlestick + spread band
    # ═══════════════════════════════════════════════════════════════
    bt, op, hi, lo, cl, vol = build_candles(t[vis], td.prices[vis], td.sizes[vis])
    valid_candles = ~np.isnan(op)

    fig.add_trace(go.Candlestick(
        x=bt[valid_candles],
        open=op[valid_candles],
        high=hi[valid_candles],
        low=lo[valid_candles],
        close=cl[valid_candles],
        increasing_line_color="#26a69a",
        decreasing_line_color="#ef5350",
        name="Price",
        showlegend=True,
    ), row=2, col=1)

    # Spread band (bid-ask)
    bid = signals["mid"][vis_ds] - signals["spread"][vis_ds] / 2
    ask = signals["mid"][vis_ds] + signals["spread"][vis_ds] / 2
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=ask, mode="lines",
        line=dict(width=0), showlegend=False, hoverinfo="skip",
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=bid, mode="lines",
        line=dict(width=0), fill="tonexty",
        fillcolor="rgba(33,150,243,0.08)",
        name="Bid-Ask spread", showlegend=True, hoverinfo="skip",
    ), row=2, col=1)

    # Mid-price thin reference
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["mid"][vis_ds],
        mode="lines", line=dict(color="#78909c", width=0.8),
        name="Mid", opacity=0.5,
    ), row=2, col=1)

    # Entry/exit markers on zoomed candle panel
    for tr in event_trades:
        et = tr.get("entry_t_sec")
        xt = tr.get("exit_t_sec")
        ep = tr.get("entry_price", 0)
        xp = tr.get("exit_price", 0)
        is_win = tr.get("pnl_pct", 0) > 0
        color = "#2e7d32" if is_win else "#c62828"
        if et is not None and x_min <= et <= x_max:
            fig.add_trace(go.Scatter(
                x=[et], y=[ep],
                mode="markers", marker=dict(symbol="triangle-up", size=8,
                                            color=color, line=dict(width=0.5, color="black")),
                name="Entry", showlegend=False,
            ), row=2, col=1)
        if xt is not None and x_min <= xt <= x_max:
            fig.add_trace(go.Scatter(
                x=[xt], y=[xp],
                mode="markers", marker=dict(symbol="triangle-down", size=8,
                                            color=color, line=dict(width=0.5, color="black")),
                name="Exit", showlegend=False,
            ), row=2, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 3: lambda_hat
    # ═══════════════════════════════════════════════════════════════
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["lambda_hat"][vis_ds],
        mode="lines", line=dict(color="#1976d2", width=1),
        name="lambda_hat",
    ), row=3, col=1)

    fig.add_hline(y=signals["lambda_ref"], line_color="#757575",
                  line_dash="dot", line_width=1,
                  annotation_text=f"lambda_ref={signals['lambda_ref']:.2f}",
                  annotation_position="top right",
                  row=3, col=1)
    fig.add_hline(y=signals["lambda_ref"] * EPG_K,
                  line_color="purple", line_dash="dash", line_width=1,
                  annotation_text=f"T_event thresh ({EPG_K}x)",
                  annotation_position="top right",
                  row=3, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 4: E(t) + Edot(t) on secondary y
    # ═══════════════════════════════════════════════════════════════
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["E"][vis_ds],
        mode="lines", line=dict(color="#e65100", width=1),
        name="E(t)",
    ), row=4, col=1, secondary_y=False)

    fig.add_hline(y=E_MIN, line_color="#e65100", line_dash="dash",
                  line_width=1, opacity=0.6,
                  annotation_text=f"E_min={E_MIN}",
                  row=4, col=1)

    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["Edot"][vis_ds],
        mode="lines", line=dict(color="#1b5e20", width=0.8),
        name="Edot(t)", opacity=0.7,
    ), row=4, col=1, secondary_y=True)

    fig.add_hline(y=THETA_SLOPE, line_color="#1b5e20", line_dash="dash",
                  line_width=1, opacity=0.5,
                  annotation_text=f"theta_slope={THETA_SLOPE}",
                  row=4, col=1, secondary_y=True)

    # ═══════════════════════════════════════════════════════════════
    # Row 5: lambda_V + peak + threshold
    # ═══════════════════════════════════════════════════════════════
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["lambda_v"][vis_ds],
        mode="lines", line=dict(color="#0097a7", width=1),
        name="lambda_V",
    ), row=5, col=1)

    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["lambda_v_peak"][vis_ds],
        mode="lines", line=dict(color="#004d40", width=0.8, dash="dot"),
        name="Running peak", opacity=0.6,
    ), row=5, col=1)

    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["lambda_v_thresh"][vis_ds],
        mode="lines", line=dict(color="#d32f2f", width=1, dash="dash"),
        name=f"Threshold (p={EPG_P})",
    ), row=5, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 6: delta_mid_5s + Gate 3 threshold
    # ═══════════════════════════════════════════════════════════════
    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["dm5"][vis_ds],
        mode="lines", line=dict(color="#37474f", width=0.8),
        name="delta_mid_5s",
    ), row=6, col=1)

    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=signals["gate3_thresh"][vis_ds],
        mode="lines", line=dict(color="#43a047", width=1, dash="dash"),
        name=f"+Gate3 (K={GATE3_K})", opacity=0.7,
    ), row=6, col=1)

    fig.add_trace(go.Scatter(
        x=t[vis_ds], y=-signals["gate3_thresh"][vis_ds],
        mode="lines", line=dict(color="#43a047", width=1, dash="dash"),
        name="-Gate3", opacity=0.7, showlegend=False,
    ), row=6, col=1)

    fig.add_hline(y=0, line_color="gray", line_width=0.5, opacity=0.3,
                  row=6, col=1)

    # ═══════════════════════════════════════════════════════════════
    # Row 7: Volume acceleration
    # ═══════════════════════════════════════════════════════════════
    va = signals["vol_accel"]
    va_valid = ~np.isnan(va) & vis_ds
    fig.add_trace(go.Scatter(
        x=t[va_valid], y=va[va_valid],
        mode="lines", line=dict(color="#5d4037", width=0.8),
        name="vol_accel", fill="tozeroy",
        fillcolor="rgba(165,214,167,0.3)",
    ), row=7, col=1)

    fig.add_hline(y=0, line_color="#d32f2f", line_dash="dash",
                  line_width=1, opacity=0.6,
                  annotation_text="EXIT 3 threshold",
                  row=7, col=1)

    # ── Layout ──
    n_trades = len(event_trades)
    total_bps = sum(tr.get("pnl_pct", 0) for tr in event_trades)
    n_wins = sum(1 for tr in event_trades if tr.get("pnl_pct", 0) > 0)
    win_rate = n_wins / n_trades * 100 if n_trades > 0 else 0
    mean_bps = total_bps / n_trades if n_trades > 0 else 0

    fig.update_layout(
        title=dict(
            text=(
                f"<b>{td.ticker} — {td.date}</b>  |  "
                f"{n_trades} trades ({n_wins}W/{n_trades - n_wins}L, {win_rate:.0f}%)  |  "
                f"Total: {total_bps:.2f}%  |  Mean: {mean_bps:.2f}%/trade"
            ),
            font=dict(size=14),
        ),
        height=1600,
        width=1400,
        template="plotly_white",
        xaxis_title="Time (seconds from first trade)",
        xaxis7_title="Time (seconds from first trade)",
        xaxis_rangeslider_visible=False,
        xaxis2_rangeslider_visible=False,
        legend=dict(
            orientation="h", yanchor="bottom", y=-0.05,
            xanchor="center", x=0.5, font=dict(size=10),
        ),
        hovermode="x unified",
    )

    # Y-axis labels
    fig.update_yaxes(title_text="Price ($)", row=2, col=1)
    fig.update_yaxes(title_text="TPS", type="log", row=3, col=1)
    fig.update_yaxes(title_text="E(t)", row=4, col=1, secondary_y=False)
    fig.update_yaxes(title_text="Edot(t)", row=4, col=1, secondary_y=True)
    fig.update_yaxes(title_text="lambda_V ($/s)", row=5, col=1)
    fig.update_yaxes(title_text="delta_mid ($)", row=6, col=1)
    fig.update_yaxes(title_text="shares/s^2", row=7, col=1)

    # Set x range for zoomed panels (rows 2-7)
    fig.update_xaxes(range=[x_min, x_max], row=2, col=1)

    # Save
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output_path), include_plotlyjs="cdn")
    print(f"  Chart saved: {output_path}")


# ══════════════════════════════════════════════════════════════════════
#  Event selection
# ══════════════════════════════════════════════════════════════════════

def select_representative_events(trades, n=10):
    """Pick diverse events by total net PnL: best, worst, and spread across the middle."""
    # Group trades by event
    by_event = {}
    for t in trades:
        key = (t["ticker"], t["date"])
        by_event.setdefault(key, []).append(t)

    # Compute per-event net PnL and trade count
    event_stats = []
    for key, event_trades in by_event.items():
        total_net = sum(tr.get("pnl_pct", 0) for tr in event_trades)
        event_stats.append({
            "key": key,
            "total_net": total_net,
            "n_trades": len(event_trades),
        })

    # Sort by total net PnL
    event_stats.sort(key=lambda x: x["total_net"])

    # Pick: worst 2, best 2, evenly spaced from middle
    picks = []
    if len(event_stats) >= n:
        # worst 2
        picks.extend(event_stats[:2])
        # best 2
        picks.extend(event_stats[-2:])
        # fill rest from middle, evenly spaced
        remaining = n - 4
        middle = event_stats[2:-2]
        if middle and remaining > 0:
            indices = np.linspace(0, len(middle) - 1, remaining, dtype=int)
            picks.extend([middle[i] for i in indices])
    else:
        picks = event_stats

    # Return list of (key, event_trades) tuples
    result = []
    seen = set()
    for p in picks:
        if p["key"] not in seen:
            seen.add(p["key"])
            result.append((p["key"], by_event[p["key"]]))
    return result[:n]


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser(description="Interactive signal diagnostic charts")
    parser.add_argument("--trades-file", type=str,
                        default=str(Path(__file__).resolve().parent.parent
                                    / "results" / "phase_e_v6" / "backtest_val_trades.json"))
    parser.add_argument("--n-charts", type=int, default=10)
    parser.add_argument("--specific", type=str, default=None,
                        help="Specific ticker_date (e.g. AAPL_2024-01-15)")
    args = parser.parse_args()

    # Configs
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Per-event Hawkes params
    phase_a_path = (Path(__file__).resolve().parent.parent
                    / "results" / "phase_a" / "production_fit_results.json")
    per_event_params = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    rho_E = hawkes_median.get("rho", 0.9999)

    # Load trade results
    with open(args.trades_file) as f:
        all_trades = json.load(f)
    print(f"Loaded {len(all_trades)} trades")

    # Group all trades by event
    trades_by_event = {}
    for t in all_trades:
        key = (t["ticker"], t["date"])
        trades_by_event.setdefault(key, []).append(t)

    # Select events
    if args.specific:
        parts = args.specific.split("_", 1)
        key = (parts[0], parts[1])
        if key not in trades_by_event:
            print(f"No trades found for {args.specific}")
            return
        selected_events = [(key, trades_by_event[key])]
    else:
        selected_events = select_representative_events(all_trades, args.n_charts)

    print(f"Generating {len(selected_events)} interactive charts...\n")

    for i, (key, event_trades) in enumerate(selected_events):
        ticker, date = key
        total_net = sum(tr.get("pnl_pct", 0) for tr in event_trades)
        n_wins = sum(1 for tr in event_trades if tr.get("pnl_pct", 0) > 0)
        print(f"[{i+1}/{len(selected_events)}] {ticker} {date} "
              f"({len(event_trades)} trades, {n_wins}W, {total_net:.2f}%)")

        fp = per_event_params.get(key, hawkes_median)

        try:
            from data.schemas.mom_db import FILTERED_DIR
            candidates = list(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
            mom_pct = float(candidates[0].name.split("_")[-1]) if candidates else 50.0

            signals = replay_all_signals(ticker, date, mom_pct, fp, rho_E, q_bar_cfg)

            safe_name = f"{ticker}_{date}"
            output_path = CHART_DIR / f"signal_{safe_name}.html"
            generate_chart(signals, event_trades, output_path)

        except Exception as e:
            print(f"  ERROR: {e}")
            import traceback
            traceback.print_exc()

    print(f"\nDone. Charts in: {CHART_DIR}")


if __name__ == "__main__":
    main()
