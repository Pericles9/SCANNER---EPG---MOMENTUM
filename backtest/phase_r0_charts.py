#!/usr/bin/env python3
"""R0 — Per-event Plotly charts for EPG-Rapid first_pass entry.

Reads:  backtest/results/phase_r0/rapid_r0/per_trade.json
Writes: backtest/results/phase_r0/event_charts/{TICKER}_{DATE}.html
        backtest/results/phase_r0/event_charts/index.html

Panel layout:
  1. Price (10s OHLC) + entry/exit markers
  2. Gate intensity E(t) = lambda_V
  3. EPG gate state trace (step, per tick) + entry vline
  4. EPG gate state colored bands
"""
from __future__ import annotations

import json
import math
import sys
import traceback as tb_module
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent
sys.path.insert(0, str(BACKTEST))

from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from data.loaders.trades import (
    load_trades, list_events, _session_ns_bounds,
    compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online
from core.features.luld_halt_detection import detect_luld_halts

REPO = BACKTEST.parent
RESULTS_DIR = BACKTEST / "results" / "phase_r0"
CHART_DIR = RESULTS_DIR / "event_charts"

EPG_K = 5
EPG_TAU = 300.0
EPG_WARMUP = 300.0
P_OPEN = 0.65
P_CLOSE = 0.65
COLD_START_SIZE = 1000
REFIT_INTERVAL = 50
REFIT_WINDOW = 10000
BETA_FIXED = 0.1
HALT_GAP_THRESHOLD = 60.0
SCANNER_THRESHOLD = 0.30

_STATE_FILL = {
    "INACTIVE": "rgba(200,200,200,0.20)",
    "WARMUP":   "rgba(255,179,71,0.25)",
    "PASS":     "rgba(100,200,100,0.22)",
    "FAIL":     "rgba(220,80,80,0.22)",
}
_STATE_SOLID = {
    "INACTIVE": "rgba(190,190,190,0.90)",
    "WARMUP":   "rgba(255,165,0,0.90)",
    "PASS":     "rgba(60,180,60,0.90)",
    "FAIL":     "rgba(210,60,60,0.90)",
}
_STATE_NUMERIC = {
    GateState.INACTIVE: 0.0,
    GateState.WARMUP:   0.33,
    GateState.FAIL:     0.67,
    GateState.PASS:     1.0,
}


# ── Hawkes replay (self-contained, C3 halt-gap pause) ────────────────────────

def _build_halt_intervals(td) -> list:
    try:
        import pandas as _pd
        df = _pd.DataFrame(
            {"price": td.prices},
            index=_pd.to_datetime(td.timestamps, unit="ns"),
        )
        hw_list = detect_luld_halts(df, price_col="price")
        if not hw_list:
            return []
        t0 = int(td.timestamps[0])
        return [
            ((hw.start.value - t0) / NS_PER_SECOND,
             (hw.end.value - t0) / NS_PER_SECOND)
            for hw in hw_list
        ]
    except Exception:
        return []


def _hawkes_replay(t_sec, sides, rho, lambda_ref, init_params, rho_E,
                   lam_buy_out, lam_sell_out, E_out, halt_intervals=None):
    N = len(t_sec)
    if N == 0:
        return None

    _halt_ivs = halt_intervals or []
    cold_end = min(COLD_START_SIZE, N)
    init_arr = np.array([
        init_params["alpha_buy_self"], init_params["alpha_sell_self"],
        init_params["mu_buy"], init_params["mu_sell"],
    ])

    if cold_end < 100:
        from core.hawkes.engine import hawkes_replay_fixed_beta
        E_tmp = np.zeros(N, dtype=np.float64)
        hawkes_replay_fixed_beta(
            t_sec, sides,
            init_params["alpha_buy_self"], 0.0,
            init_params["alpha_sell_self"], 0.0,
            init_params["mu_buy"], init_params["mu_sell"],
            init_params["beta"], rho_E,
            lam_buy_out, lam_sell_out, E_tmp, E_tmp,
        )
        mu_t = max(init_params["mu_buy"] + init_params["mu_sell"], 1e-10)
        E_out[:] = (lam_buy_out + lam_sell_out) / mu_t
        return None

    params = fit_hawkes_forgetting(
        t_sec=t_sec[:cold_end], sides=sides[:cold_end],
        rho=rho, lambda_ref=lambda_ref, T=float(t_sec[cold_end - 1]),
        init_params=init_arr, n_restarts=5, beta_fixed=BETA_FIXED,
    )

    refit_pts = list(range(cold_end + REFIT_INTERVAL, N + 1, REFIT_INTERVAL))
    if refit_pts and refit_pts[-1] < N:
        refit_pts.append(N)
    elif not refit_pts and N > cold_end:
        refit_pts = [N]

    if refit_pts:
        chunk_s = [0, cold_end] + refit_pts[:-1]
        chunk_e = [cold_end] + refit_pts
    else:
        chunk_s = [0]
        chunk_e = [N]

    R_buy = R_sell = 0.0
    E_prev = 1.0
    Edot_ema = 0.0

    for ci in range(len(chunk_e)):
        cs, ce = chunk_s[ci], chunk_e[ci]
        if ci > 0:
            ws = max(0, ce - REFIT_WINDOW)
            params = fit_online(
                t_sec=t_sec[ws:ce], sides=sides[ws:ce],
                rho=rho, lambda_ref=lambda_ref, prev_params=params,
                T=float(t_sec[ce - 1]), n_restarts=1, beta_fixed=BETA_FIXED,
            )
        mu_t = max(params.mu_buy + params.mu_sell, 1e-10)

        for i in range(cs, ce):
            if i == 0:
                lb = max(params.mu_buy, 0.0)
                ls = max(params.mu_sell, 0.0)
                E_out[0] = (lb + ls) / mu_t
                lam_buy_out[0] = lb
                lam_sell_out[0] = ls
                R_buy = 1.0 if sides[0] == 1 else 0.0
                R_sell = 0.0 if sides[0] == 1 else 1.0
                E_prev = E_out[0]
            else:
                dt = t_sec[i] - t_sec[i - 1]
                dt_eff = dt
                if _halt_ivs and dt_eff > HALT_GAP_THRESHOLD:
                    tp, tc = t_sec[i - 1], t_sec[i]
                    for hs, he in _halt_ivs:
                        if tp < he and tc > hs:
                            dt_eff = 1e-6
                            break
                if dt_eff > 0:
                    dec = np.exp(-params.beta * dt_eff)
                    R_buy *= dec
                    R_sell *= dec

                lb = max(0.0, params.mu_buy + params.alpha_buy_self * R_buy)
                ls = max(0.0, params.mu_sell + params.alpha_sell_self * R_sell)
                E_out[i] = (lb + ls) / mu_t
                lam_buy_out[i] = lb
                lam_sell_out[i] = ls
                E_prev = E_out[i]
                R_buy += 1.0 if sides[i] == 1 else 0.0
                R_sell += 0.0 if sides[i] == 1 else 1.0

    return params


# ── 10-second OHLC bars ───────────────────────────────────────────────────────

def _build_10s_bars(timestamps_ns: np.ndarray, prices: np.ndarray, session_start_ns: int):
    BAR_NS = 10 * NS_PER_SECOND
    N = len(timestamps_ns)
    if N == 0:
        empty = np.array([], dtype=np.float64)
        return np.array([], dtype=np.int64), empty, empty, empty, empty
    bar_idx = ((timestamps_ns.astype(np.int64) - session_start_ns) // BAR_NS).astype(np.int64)
    unique_bars = np.unique(bar_idx)
    starts, opens, highs, lows, closes = [], [], [], [], []
    for bi in unique_bars:
        mask = bar_idx == bi
        bp = prices[mask]
        starts.append(int(session_start_ns + bi * BAR_NS))
        opens.append(float(bp[0]))
        highs.append(float(np.max(bp)))
        lows.append(float(np.min(bp)))
        closes.append(float(bp[-1]))
    return (
        np.array(starts, dtype=np.int64),
        np.array(opens), np.array(highs),
        np.array(lows), np.array(closes),
    )


def _build_state_intervals(t_rel: list, states: list) -> list:
    if not t_rel:
        return []
    intervals = []
    cur_state, cur_start = states[0], t_rel[0]
    for i in range(1, len(t_rel)):
        if states[i] != cur_state:
            intervals.append((cur_start, t_rel[i], cur_state))
            cur_state = states[i]
            cur_start = t_rel[i]
    intervals.append((cur_start, t_rel[-1], cur_state))
    return intervals


# ── Worker ────────────────────────────────────────────────────────────────────

def _collect_event_diag_r0(args: dict) -> dict:
    """Re-run Hawkes + EPG for one traded event; collect chart arrays."""
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    p_open = args.get("p_open", P_OPEN)
    p_close = args.get("p_close", P_CLOSE)
    # Trade info from per_trade.json (provided by caller)
    entry_ts = args.get("entry_ts")      # ns
    exit_ts = args.get("exit_ts")        # ns
    entry_price = args.get("entry_price")
    exit_price = args.get("exit_price")
    pnl_pct = args.get("pnl_pct")
    hold_sec = args.get("hold_sec")
    entry_lag_sec = args.get("entry_lag_sec")
    session_bucket = args.get("session_bucket", "unknown")
    exit_reason = args.get("exit_reason", "unknown")

    base = {"ticker": ticker, "session_date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td is None or td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades
        start_ns, _ = _session_ns_bounds(date)

        # Find scanner hit (first >=30% intraday)
        from data.loaders.prev_close import get_prev_close
        prev_close = get_prev_close(ticker, date)
        if prev_close is None or prev_close <= 0:
            return {**base, "status": "skipped", "reason": "missing_prev_close"}

        t_scanner_hit_sec = float(td.t_sec[0])
        t_scanner_hit_ns = int(td.timestamps[0])
        for i in range(N):
            if (td.prices[i] - prev_close) / prev_close >= SCANNER_THRESHOLD:
                t_scanner_hit_sec = float(td.t_sec[i])
                t_scanner_hit_ns = int(td.timestamps[i])
                break

        # Lee-Ready sides
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
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
        sides = ofi.sides

        # Hawkes replay
        halt_intervals = _build_halt_intervals(td)
        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_ev_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (global_lref
                      if (math.isnan(per_ev_lref) or per_ev_lref <= 0)
                      else per_ev_lref)

        cold_params = _hawkes_replay(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out,
            halt_intervals=halt_intervals or None,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # EPG gate
        lref_epg = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=lref_epg, k_multiplier=EPG_K)
        if cold_params is not None:
            lref2 = cold_params.mu_buy + cold_params.mu_sell
            if lref2 > 0:
                anchor.set_lambda_ref(lref2)

        gate = ParticipationGate(
            half_life_seconds=EPG_TAU,
            peak_threshold_p=p_open,
            warmup_seconds=EPG_WARMUP,
            p_open=p_open,
            p_close=p_close,
        )

        epg_states = []
        t_event_fired = False
        t_event_sec = 0.0

        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None and not t_event_fired:
                gate.activate(t_ev)
                t_event_fired = True
                t_event_sec = float(td.t_sec[i])
            dv = float(td.prices[i]) * float(td.sizes[i])
            epg_states.append(gate.update(dv, td.t_sec[i]))

        if not t_event_fired:
            return {**base, "status": "skipped", "reason": "no_t_event"}

        # Per-tick relative times (seconds from scanner hit)
        tick_t_rel_all = td.t_sec - t_scanner_hit_sec
        t_event_rel = t_event_sec - t_scanner_hit_sec
        t_warmup_rel = (t_event_sec + EPG_WARMUP) - t_scanner_hit_sec

        # Entry / exit relative times from per_trade.json timestamps
        entry_t_rel = None
        exit_t_rel = None
        if entry_ts is not None:
            entry_t_rel = (entry_ts - t_scanner_hit_ns) / NS_PER_SECOND
        if exit_ts is not None:
            exit_t_rel = (exit_ts - t_scanner_hit_ns) / NS_PER_SECOND

        # Chart window: from 2 min before anchor fire to 5 min after exit
        win_start_rel = t_event_rel - 120.0
        if exit_t_rel is not None:
            win_end_rel = max(exit_t_rel + 300.0,
                              (entry_t_rel or t_event_rel) + 900.0)
        else:
            win_end_rel = t_event_rel + 3600.0

        # Gate state trace (numeric per tick)
        gate_state_numeric = [_STATE_NUMERIC.get(s, 0.0) for s in epg_states]
        state_names = [s.name for s in epg_states]

        # State intervals for background shading
        state_intervals = _build_state_intervals(
            tick_t_rel_all.tolist(), state_names
        )

        # Apply window mask
        tick_mask = (
            (tick_t_rel_all >= win_start_rel) & (tick_t_rel_all <= win_end_rel)
        )
        tick_t_rel = tick_t_rel_all[tick_mask].tolist()
        tick_E = E_out[tick_mask].tolist()
        tick_gate_state = np.array(gate_state_numeric)[tick_mask].tolist()

        # 10s OHLC bars (windowed)
        b10s_starts, b10s_o, b10s_h, b10s_l, b10s_c = _build_10s_bars(
            td.timestamps, td.prices, start_ns,
        )
        if len(b10s_starts) > 0:
            b10s_t_rel_all = (
                b10s_starts.astype(np.float64) - t_scanner_hit_ns
            ) / NS_PER_SECOND
            bm = (b10s_t_rel_all >= win_start_rel) & (b10s_t_rel_all <= win_end_rel)
            b10s_t = b10s_t_rel_all[bm].tolist()
            b10s_o_w = b10s_o[bm].tolist()
            b10s_h_w = b10s_h[bm].tolist()
            b10s_l_w = b10s_l[bm].tolist()
            b10s_c_w = b10s_c[bm].tolist()
        else:
            b10s_t = b10s_o_w = b10s_h_w = b10s_l_w = b10s_c_w = []

        # Gate chatter count
        n_passtofail = 0
        prev_s = GateState.INACTIVE
        for s in epg_states:
            if prev_s == GateState.PASS and s != GateState.PASS:
                n_passtofail += 1
            prev_s = s

        return {
            **base,
            "status": "event",
            "has_trade": entry_ts is not None,
            "pnl_pct": pnl_pct,
            "hold_sec": hold_sec,
            "entry_lag_sec": entry_lag_sec,
            "entry_price": entry_price,
            "exit_price": exit_price,
            "session_bucket": session_bucket,
            "exit_reason": exit_reason,
            "n_passtofail": n_passtofail,
            "t_event_rel": round(t_event_rel, 1),
            "entry_t_rel": round(entry_t_rel, 1) if entry_t_rel is not None else None,
            "exit_t_rel": round(exit_t_rel, 1) if exit_t_rel is not None else None,
            "_chart": {
                "win_start_rel": win_start_rel,
                "win_end_rel": win_end_rel,
                "state_intervals": state_intervals,
                "b10s_t": b10s_t,
                "b10s_o": b10s_o_w,
                "b10s_h": b10s_h_w,
                "b10s_l": b10s_l_w,
                "b10s_c": b10s_c_w,
                "tick_t_rel": tick_t_rel,
                "tick_E": tick_E,
                "tick_gate_state": tick_gate_state,
                "t_event_rel": t_event_rel,
                "t_warmup_rel": t_warmup_rel,
                "entry_t_rel": entry_t_rel,
                "exit_t_rel": exit_t_rel,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "entry_lag_sec": entry_lag_sec,
                "hold_sec": hold_sec,
                "p_open": p_open,
                "p_close": p_close,
                "n_passtofail": n_passtofail,
                "session_bucket": session_bucket,
                "exit_reason": exit_reason,
            },
        }

    except Exception as e:
        return {
            **base,
            "status": "error",
            "error": str(e),
            "traceback": tb_module.format_exc(),
        }


# ── Chart generation ──────────────────────────────────────────────────────────

def generate_chart(diag: dict, out_dir: Path) -> Path | None:
    if not diag.get("has_trade"):
        return None

    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("plotly not installed — skipping chart generation")
        return None

    cd = diag["_chart"]
    ticker = diag["ticker"]
    date = diag["session_date"]
    pnl = cd["pnl_pct"]
    hold = cd["hold_sec"]
    lag = cd["entry_lag_sec"]
    chatter = cd["n_passtofail"]
    session = cd["session_bucket"]
    p_open_v = cd["p_open"]
    p_close_v = cd["p_close"]

    win_start = cd["win_start_rel"]
    win_end = cd["win_end_rel"]
    state_intervals = cd["state_intervals"]
    t_event_rel = cd["t_event_rel"]
    t_warmup_rel = cd["t_warmup_rel"]
    entry_t_rel = cd["entry_t_rel"]
    exit_t_rel = cd["exit_t_rel"]
    entry_price = cd["entry_price"]
    exit_price = cd["exit_price"]

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.38, 0.22, 0.22, 0.18],
        subplot_titles=[
            "Price (10s OHLC)",
            f"Gate Intensity  E(t) = lambda_V  [p_open={p_open_v}, p_close={p_close_v}]",
            "EPG Gate State Trace",
            "EPG Gate State Bands",
        ],
    )

    # Background shading on panels 1 & 2
    for t_start, t_end, state in state_intervals:
        fill = _STATE_FILL.get(state, "rgba(200,200,200,0.15)")
        for row in (1, 2):
            fig.add_shape(
                type="rect",
                x0=t_start, x1=t_end,
                y0=0, y1=1,
                xref="x", yref="y domain",
                row=row, col=1,
                fillcolor=fill,
                opacity=1.0,
                layer="below",
                line_width=0,
            )

    # Panel 1: 10s OHLC
    b10s_t = cd["b10s_t"]
    if b10s_t:
        fig.add_trace(
            go.Candlestick(
                x=[t + 5.0 for t in b10s_t],
                open=cd["b10s_o"],
                high=cd["b10s_h"],
                low=cd["b10s_l"],
                close=cd["b10s_c"],
                name="Price",
                increasing_line_color="#2ca02c",
                decreasing_line_color="#d62728",
                showlegend=False,
            ),
            row=1, col=1,
        )

    # Entry marker (green ▲)
    if entry_t_rel is not None and entry_price is not None:
        fig.add_trace(
            go.Scatter(
                x=[entry_t_rel], y=[entry_price],
                mode="markers",
                marker=dict(symbol="triangle-up", size=14, color="lime",
                            line=dict(color="darkgreen", width=1.5)),
                name="Entry",
            ),
            row=1, col=1,
        )

    # Exit marker (green ▼ win / red ▼ loss)
    if exit_t_rel is not None and exit_price is not None:
        win = (pnl or 0.0) >= 0
        exit_color = "#00cc44" if win else "#cc2200"
        fig.add_trace(
            go.Scatter(
                x=[exit_t_rel], y=[exit_price],
                mode="markers",
                marker=dict(symbol="triangle-down", size=14, color=exit_color,
                            line=dict(color="black", width=1.0)),
                name=f"Exit ({cd.get('exit_reason','?')})",
            ),
            row=1, col=1,
        )

    # Vertical reference lines (all panels)
    fig.add_vline(x=0.0, line_color="royalblue", line_width=1.5,
                  annotation_text="scanner", annotation_position="top right")
    fig.add_vline(x=t_event_rel, line_color="orangered", line_width=1.8,
                  annotation_text="T0", annotation_position="top left")
    fig.add_vline(x=t_warmup_rel, line_color="orange", line_width=1.2,
                  line_dash="dash", annotation_text="warmup end",
                  annotation_position="top left")
    if entry_t_rel is not None:
        fig.add_vline(x=entry_t_rel, line_color="green", line_width=1.8,
                      annotation_text="entry", annotation_position="top right")
    if exit_t_rel is not None:
        exit_color_line = "#007722" if (pnl or 0.0) >= 0 else "#cc0000"
        fig.add_vline(x=exit_t_rel, line_color=exit_color_line, line_width=1.2,
                      line_dash="dot", annotation_text="exit",
                      annotation_position="top right")

    # Panel 2: E(t)
    tick_t_rel = cd["tick_t_rel"]
    tick_E = cd["tick_E"]
    if tick_t_rel:
        fig.add_trace(
            go.Scatter(
                x=tick_t_rel, y=tick_E,
                mode="lines",
                line=dict(color="steelblue", width=1.3),
                name="E(t)",
            ),
            row=2, col=1,
        )

    # Panel 3: gate state trace (step function)
    tick_gate_state = cd["tick_gate_state"]
    if tick_t_rel and tick_gate_state:
        # PASS=1.0 segments in green, non-PASS in grey
        gv = np.array(tick_gate_state)
        pass_y = [float(v) if v == 1.0 else None for v in gv]
        other_y = [float(v) if v != 1.0 else None for v in gv]

        if any(v is not None for v in pass_y):
            fig.add_trace(
                go.Scatter(
                    x=tick_t_rel, y=pass_y,
                    mode="lines",
                    line=dict(color="limegreen", width=2.5, shape="hv"),
                    name="Gate PASS",
                    connectgaps=False,
                ),
                row=3, col=1,
            )
        if any(v is not None for v in other_y):
            fig.add_trace(
                go.Scatter(
                    x=tick_t_rel, y=other_y,
                    mode="lines",
                    line=dict(color="salmon", width=1.5, shape="hv"),
                    name="Gate non-PASS",
                    connectgaps=False,
                ),
                row=3, col=1,
            )

        # Threshold line at PASS=1.0 boundary
        fig.add_shape(
            type="line",
            x0=win_start, x1=win_end,
            y0=0.85, y1=0.85,
            line=dict(color="darkgreen", width=1.0, dash="dash"),
            row=3, col=1,
        )

        # Vertical line at entry tick
        if entry_t_rel is not None:
            fig.add_vline(
                x=entry_t_rel, line_color="green", line_width=2.0,
                row=3, col=1,
            )

        # State labels on y-axis
        fig.update_yaxes(
            tickvals=[0.0, 0.33, 0.67, 1.0],
            ticktext=["INACTIVE", "WARMUP", "FAIL", "PASS"],
            row=3, col=1,
        )

    # Panel 4: gate state colored bands
    for t_start, t_end, state in state_intervals:
        fig.add_shape(
            type="rect",
            x0=t_start, x1=t_end,
            y0=0, y1=1,
            xref="x", yref="y domain",
            row=4, col=1,
            fillcolor=_STATE_SOLID.get(state, "rgba(200,200,200,0.85)"),
            opacity=1.0,
            layer="below",
            line_width=0,
        )
    for state, color in _STATE_SOLID.items():
        fig.add_trace(
            go.Scatter(
                x=[None], y=[None],
                mode="markers",
                marker=dict(size=10, color=color, symbol="square"),
                name=state,
                showlegend=True,
            ),
            row=4, col=1,
        )

    # Layout
    pnl_str = f"{pnl:+.2f}%" if pnl is not None else "—"
    lag_str = f"{lag:.0f}s" if lag is not None else "—"
    hold_str = f"{hold:.0f}s" if hold is not None else "—"

    fig.update_layout(
        title=dict(
            text=(
                f"{ticker}  {date}  [{session}]  —  "
                f"PnL: {pnl_str}  hold: {hold_str}  "
                f"lag: {lag_str}  chatter: {chatter}"
            ),
            font=dict(size=13),
        ),
        height=940,
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    fig.update_xaxes(range=[win_start, win_end])
    fig.update_xaxes(title_text="Seconds from scanner hit", row=4, col=1)
    fig.update_yaxes(title_text="Price ($)", row=1, col=1)
    fig.update_yaxes(title_text="E(t)", row=2, col=1)
    fig.update_yaxes(title_text="State", range=[-0.1, 1.1], row=3, col=1)
    fig.update_yaxes(title_text="State", range=[0, 1],
                     showticklabels=False, row=4, col=1)

    chart_path = out_dir / f"{ticker}_{date}.html"
    fig.write_html(str(chart_path), include_plotlyjs="cdn",
                   config={"responsive": True})
    return chart_path


# ── Index HTML ────────────────────────────────────────────────────────────────

def generate_index(events: list[dict], chart_dir: Path,
                   p_open: float = P_OPEN, p_close: float = P_CLOSE) -> None:
    rows = []
    for ev in events:
        if ev.get("status") != "event" or not ev.get("has_trade"):
            continue
        chart_file = f"{ev['ticker']}_{ev['session_date']}.html"
        rows.append({
            "ticker": ev["ticker"],
            "date": ev["session_date"],
            "pnl_pct": ev.get("pnl_pct"),
            "hold_sec": ev.get("hold_sec"),
            "entry_lag_sec": ev.get("entry_lag_sec"),
            "n_passtofail": ev.get("n_passtofail"),
            "session_bucket": ev.get("session_bucket", "?"),
            "exit_reason": ev.get("exit_reason", "?"),
            "t_event_rel": ev.get("t_event_rel"),
            "entry_t_rel": ev.get("entry_t_rel"),
            "chart": chart_file if (chart_dir / chart_file).exists() else None,
        })

    rows.sort(key=lambda r: (r["pnl_pct"] or -999.0), reverse=True)

    def _fmt(v):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.1f}"
        return str(v)

    def _pnl_color(v):
        if v is None:
            return "inherit"
        return "green" if v >= 0 else "red"

    row_html = "\n".join(
        f'<tr>'
        f'<td>{r["ticker"]}</td>'
        f'<td>{r["date"]}</td>'
        f'<td style="color:{_pnl_color(r["pnl_pct"])}">{_fmt(r["pnl_pct"])}</td>'
        f'<td>{_fmt(r["hold_sec"])}</td>'
        f'<td>{_fmt(r["entry_lag_sec"])}</td>'
        f'<td>{_fmt(r["n_passtofail"])}</td>'
        f'<td>{r["session_bucket"]}</td>'
        f'<td>{r["exit_reason"]}</td>'
        f'<td>{"<a href=" + chr(34) + r["chart"] + chr(34) + " target=_blank>chart</a>" if r["chart"] else "—"}</td>'
        f'</tr>'
        for r in rows
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>EPG-Rapid R0 — Per-event Charts</title>
<style>
body {{ font-family: monospace; font-size: 13px; padding: 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: right; }}
th {{ background: #eee; cursor: pointer; user-select: none; }}
td:first-child, td:nth-child(2) {{ text-align: left; }}
tr:hover {{ background: #f5f5f5; }}
a {{ color: #1a73e8; text-decoration: none; }}
</style>
<script>
let _sortCol = 2, _sortAsc = false;
function sortTable(n) {{
  var t = document.getElementById("main");
  var rows = Array.from(t.tBodies[0].rows);
  _sortAsc = (_sortCol === n) ? !_sortAsc : false;
  _sortCol = n;
  rows.sort(function(a, b) {{
    var av = a.cells[n].innerText.replace('—',''), bv = b.cells[n].innerText.replace('—','');
    var an = parseFloat(av), bn = parseFloat(bv);
    if (!isNaN(an) && !isNaN(bn)) return _sortAsc ? an - bn : bn - an;
    return _sortAsc ? av.localeCompare(bv) : bv.localeCompare(av);
  }});
  rows.forEach(function(r) {{ t.tBodies[0].appendChild(r); }});
}}
</script>
</head>
<body>
<h2>EPG-Rapid R0 — first_pass entry  [p_open={p_open} / p_close={p_close}]</h2>
<p>
  Default sort: <b>PnL% DESC</b>.
  Click column headers to re-sort.
  entry_lag = seconds from T0 (anchor fire) to entry tick.
  chatter = total gate PASS→non-PASS transitions across full event.
</p>
<table id="main">
<thead><tr>
  <th onclick="sortTable(0)">Ticker</th>
  <th onclick="sortTable(1)">Date</th>
  <th onclick="sortTable(2)">PnL%</th>
  <th onclick="sortTable(3)">Hold (s)</th>
  <th onclick="sortTable(4)">Entry lag (s)</th>
  <th onclick="sortTable(5)">Chatter</th>
  <th onclick="sortTable(6)">Session</th>
  <th onclick="sortTable(7)">Exit reason</th>
  <th>Chart</th>
</tr></thead>
<tbody>
{row_html}
</tbody>
</table>
<p style="margin-top:16px; color:#666; font-size:12px">
  EPG-Rapid: first_pass entry — no SF, no entry_eligible(), no n_hold.<br>
  Exit: EPG PASS→FAIL (window close). EXIT_D off. LULD off.<br>
  Val split, 100-event stratified sample, seed=42.
</p>
</body>
</html>"""

    with open(chart_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    CHART_DIR.mkdir(parents=True, exist_ok=True)

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Load traded events from rapid_r0 per_trade.json
    per_trade_path = RESULTS_DIR / "rapid_r0" / "per_trade.json"
    if not per_trade_path.exists():
        print(f"ERROR: {per_trade_path} not found — run T3 rapid first", file=sys.stderr)
        sys.exit(1)
    with open(per_trade_path) as f:
        trades = json.load(f)
    print(f"Loaded {len(trades)} trades from rapid_r0/per_trade.json")

    # Val event lookup for mom_pct
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    all_events = list_events(min_mom=50.0, require_date=True)
    val_lookup = {
        (e["ticker"], e["date"]): e
        for e in all_events
        if val_start <= e["date"] < test_start
    }

    # Per-event Hawkes params
    phase_a_path = REPO / "results" / "phase_a" / "production_fit_results.json"
    per_event_params: dict = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    # Build args_list (one per trade — deduplicated on ticker/date)
    seen = set()
    args_list = []
    for tr in trades:
        key = (tr["ticker"], tr["date"])
        if key in seen:
            continue
        seen.add(key)
        ev = val_lookup.get(key)
        if ev is None:
            print(f"  WARNING: {key} not in val events — skipping")
            continue
        fp = per_event_params.get(key, hawkes_median)
        args_list.append({
            "ticker": tr["ticker"],
            "date": tr["date"],
            "mom_pct": ev["mom_pct"],
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
            # Trade info from per_trade.json
            "entry_ts": tr.get("entry_ts"),
            "exit_ts": tr.get("exit_ts"),
            "entry_price": tr.get("entry_price"),
            "exit_price": tr.get("exit_price"),
            "pnl_pct": tr.get("pnl_pct"),
            "hold_sec": tr.get("hold_sec"),
            "entry_lag_sec": tr.get("entry_lag_sec"),
            "session_bucket": tr.get("session_bucket", "unknown"),
            "exit_reason": tr.get("exit_reason", "unknown"),
        })

    print(f"Processing {len(args_list)} events (6 workers)...")
    raw_results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_collect_event_diag_r0, a): a for a in args_list}
        done = 0
        for future in as_completed(futures):
            done += 1
            r = future.result()
            raw_results.append(r)
            if done % 10 == 0:
                print(f"  {done}/{len(args_list)} events processed...")

    event_results = [r for r in raw_results if r.get("status") == "event"]
    traded_results = [r for r in event_results if r.get("has_trade")]
    errors = [r for r in raw_results if r.get("status") == "error"]

    if errors:
        print(f"  {len(errors)} errors:")
        for e in errors[:5]:
            print(f"    {e['ticker']} {e.get('session_date','?')}: {e.get('error','')[:120]}")

    print(f"Events: {len(event_results)} processed, {len(traded_results)} with trades")
    print(f"Generating {len(traded_results)} charts...")

    n_ok = 0
    for i, r in enumerate(traded_results):
        try:
            p = generate_chart(r, CHART_DIR)
            if p:
                n_ok += 1
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(traded_results)} charts generated")
        except Exception as e:
            print(f"  Chart failed {r['ticker']} {r['session_date']}: {e}")

    generate_index(event_results, CHART_DIR, p_open=P_OPEN, p_close=P_CLOSE)
    print(f"{n_ok}/{len(traded_results)} charts OK -> {CHART_DIR}")
    print(f"Index -> {CHART_DIR}/index.html")
    print("R0 CHARTS COMPLETE")


if __name__ == "__main__":
    main()
