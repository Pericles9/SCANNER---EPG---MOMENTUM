#!/usr/bin/env python3
"""R1-D (REDO) — Entry Lag Diagnostic Charts (Plotly HTML).

H1: EventAnchor fires late (after scanner hit).
H2: Q̃ smoothing (rho_fast=0.90) delays 3 consecutive qualifying bars.

Source: results/phase_r1/symmetric_p65/per_trade.json (81 traded events).
Replay is for signal extraction only — do not re-run the backtest.

Outputs:
  results/phase_r1/entry_lag_diagnosis.json
  results/phase_r1/diagnostic_charts/{TICKER}_{DATE}.html
  results/phase_r1/diagnostic_charts/index.html  (default sort: first_3consec DESC)
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
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from setup_filter import run_setup_filter, _build_1min_bars
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online
from core.filters.rapid_entry import Q_THRESHOLD
from core.features.luld_halt_detection import detect_luld_halts

EPG_K = 5
EPG_TAU = 300.0
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000
REFIT_INTERVAL = 50
REFIT_WINDOW = 10000
BETA_FIXED = 0.1
HALT_GAP_THRESHOLD = 60.0

N_HOLD = 3
P_OPEN = 0.65
P_CLOSE = 0.65
SCANNER_THRESHOLD = 0.30

REPO = Path(__file__).resolve().parent.parent
RESULTS_DIR = BACKTEST / "results" / "phase_r1"
CHART_DIR = RESULTS_DIR / "diagnostic_charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _build_10s_bars(timestamps_ns: np.ndarray, prices: np.ndarray, session_start_ns: int):
    """Bucket trades into 10s bars aligned to session_start_ns."""
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
    """Convert per-tick state list to [(t_start, t_end, state), ...]."""
    if not t_rel:
        return []
    intervals = []
    cur_state = states[0]
    cur_start = t_rel[0]
    for i in range(1, len(t_rel)):
        if states[i] != cur_state:
            intervals.append((cur_start, t_rel[i], cur_state))
            cur_state = states[i]
            cur_start = t_rel[i]
    intervals.append((cur_start, t_rel[-1], cur_state))
    return intervals


# ── Hawkes replay ─────────────────────────────────────────────────────────────

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


def _hawkes_replay_with_refit(
    t_sec, sides, rho, lambda_ref, init_params, rho_E,
    lam_buy_out, lam_sell_out, E_out, Edot_out, n_base_out,
    halt_intervals=None,
):
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
        hawkes_replay_fixed_beta(
            t_sec, sides,
            init_params["alpha_buy_self"], 0.0,
            init_params["alpha_sell_self"], 0.0,
            init_params["mu_buy"], init_params["mu_sell"],
            init_params["beta"], rho_E,
            lam_buy_out, lam_sell_out, E_out, Edot_out,
        )
        n_base_out[:] = (init_params["alpha_buy_self"] + init_params["alpha_sell_self"]) / BETA_FIXED
        return None

    params = fit_hawkes_forgetting(
        t_sec=t_sec[:cold_end], sides=sides[:cold_end],
        rho=rho, lambda_ref=lambda_ref, T=float(t_sec[cold_end - 1]),
        init_params=init_arr, n_restarts=5, beta_fixed=BETA_FIXED,
    )

    refit_points = list(range(cold_end + REFIT_INTERVAL, N + 1, REFIT_INTERVAL))
    if refit_points and refit_points[-1] < N:
        refit_points.append(N)
    elif not refit_points and N > cold_end:
        refit_points = [N]

    if refit_points:
        chunk_starts = [0, cold_end] + refit_points[:-1]
        chunk_ends = [cold_end] + refit_points
    else:
        chunk_starts = [0]
        chunk_ends = [N]

    R_buy = 0.0
    R_sell = 0.0
    E_prev = 1.0
    Edot_ema = 0.0

    for chunk_idx in range(len(chunk_ends)):
        c_start = chunk_starts[chunk_idx]
        c_end = chunk_ends[chunk_idx]

        if chunk_idx > 0:
            w_start = max(0, c_end - REFIT_WINDOW)
            params = fit_online(
                t_sec=t_sec[w_start:c_end], sides=sides[w_start:c_end],
                rho=rho, lambda_ref=lambda_ref, prev_params=params,
                T=float(t_sec[c_end - 1]), n_restarts=1, beta_fixed=BETA_FIXED,
            )

        mu_total = max(params.mu_buy + params.mu_sell, 1e-10)
        chunk_n_base = (params.alpha_buy_self + params.alpha_sell_self) / params.beta

        for i in range(c_start, c_end):
            n_base_out[i] = chunk_n_base
            if i == 0:
                lam_b = max(params.mu_buy, 0.0)
                lam_s = max(params.mu_sell, 0.0)
                E_val = (lam_b + lam_s) / mu_total
                lam_buy_out[0] = lam_b
                lam_sell_out[0] = lam_s
                E_out[0] = E_val
                Edot_out[0] = 0.0
                R_buy = 1.0 if sides[0] == 1 else 0.0
                R_sell = 0.0 if sides[0] == 1 else 1.0
                E_prev = E_val
            else:
                dt = t_sec[i] - t_sec[i - 1]
                dt_eff = dt
                if _halt_ivs and dt_eff > HALT_GAP_THRESHOLD:
                    t_p, t_c = t_sec[i - 1], t_sec[i]
                    for h_s, h_e in _halt_ivs:
                        if t_p < h_e and t_c > h_s:
                            dt_eff = 1e-6
                            break
                if dt_eff > 0:
                    decay = np.exp(-params.beta * dt_eff)
                    R_buy *= decay
                    R_sell *= decay

                lam_b = max(0.0, params.mu_buy + params.alpha_buy_self * R_buy)
                lam_s = max(0.0, params.mu_sell + params.alpha_sell_self * R_sell)
                E_val = (lam_b + lam_s) / mu_total
                dt_cap = max(min(dt_eff, 1.0), 1e-12)
                Edot_ema = rho_E * Edot_ema + (1.0 - rho_E) * (E_val - E_prev) / dt_cap

                lam_buy_out[i] = lam_b
                lam_sell_out[i] = lam_s
                E_out[i] = E_val
                Edot_out[i] = Edot_ema

                R_buy += 1.0 if sides[i] == 1 else 0.0
                R_sell += 0.0 if sides[i] == 1 else 1.0
                E_prev = E_val

    return params


# ── Worker ────────────────────────────────────────────────────────────────────

def _collect_event_diag(args: dict) -> dict:
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    p_open = args.get("p_open", P_OPEN)
    p_close = args.get("p_close", P_CLOSE)

    base = {"ticker": ticker, "session_date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td is None or td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        prev_close = get_prev_close(ticker, date)
        if prev_close is None or prev_close <= 0:
            return {**base, "status": "skipped", "reason": "missing_prev_close"}

        N = td.n_trades
        start_ns, end_ns = _session_ns_bounds(date)

        # Scanner hit: first trade >= 30% intraday
        t_scanner_hit_sec = float(td.t_sec[0])
        t_scanner_hit_ns = int(td.timestamps[0])
        for i in range(N):
            if (td.prices[i] - prev_close) / prev_close >= SCANNER_THRESHOLD:
                t_scanner_hit_sec = float(td.t_sec[i])
                t_scanner_hit_ns = int(td.timestamps[i])
                break

        # Setup filter + 1-min bars
        sf = run_setup_filter(
            timestamps=td.timestamps,
            prices=td.prices,
            sizes=td.sizes,
            session_start_ns=start_ns,
            session_end_ns=end_ns,
        )
        _, _, _, _, _, _, bar_starts_ns = _build_1min_bars(
            td.timestamps, td.prices, td.sizes.astype(np.int64), start_ns, end_ns
        )
        n_bars = len(bar_starts_ns)
        q_tilde = sf.q_tilde
        n_q_bars = len(q_tilde)

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
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_ev_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = global_lref if (math.isnan(per_ev_lref) or per_ev_lref <= 0) else per_ev_lref

        cold_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
            halt_intervals=halt_intervals or None,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # EPG + e_peak tracking
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
        e_peak_thresholds = []
        t_event_fired = False
        t_event_sec = 0.0
        e_peak = 0.0

        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None and not t_event_fired:
                gate.activate(t_ev)
                t_event_fired = True
                t_event_sec = float(td.t_sec[i])
            dv = float(td.prices[i]) * float(td.sizes[i])
            state = gate.update(dv, td.t_sec[i])
            epg_states.append(state)
            e_val = float(E_out[i])
            if t_event_fired:
                e_peak = max(e_peak, e_val)
            e_peak_thresholds.append(p_open * e_peak)

        if not t_event_fired:
            return {**base, "status": "skipped", "reason": "no_t_event"}

        # Q̃ qualifying bar offsets
        # bar_t_sec: seconds from first trade to each bar start
        bar_t_sec = (bar_starts_ns.astype(np.float64) - float(td.timestamps[0])) / NS_PER_SECOND

        def bar_offset_rel(idx):
            if idx is None or idx >= len(bar_t_sec):
                return None
            return float(bar_t_sec[idx]) - t_scanner_hit_sec

        first_q_idx = None
        for i in range(n_q_bars):
            if q_tilde[i] >= Q_THRESHOLD:
                first_q_idx = i
                break

        first_3c_idx = None
        consec = 0
        for i in range(n_q_bars):
            if q_tilde[i] >= Q_THRESHOLD:
                consec += 1
                if consec >= N_HOLD:
                    first_3c_idx = i - (N_HOLD - 1)
                    break
            else:
                consec = 0

        first_q_offset = bar_offset_rel(first_q_idx)
        first_3c_offset = bar_offset_rel(first_3c_idx)

        # Entry state machine
        prev_state = GateState.INACTIVE
        in_position = False
        closed_today = False
        entry_t_sec = None
        entry_price = None
        n_passtofail = 0

        for i in range(N):
            cur = epg_states[i]
            if prev_state == GateState.PASS and cur != GateState.PASS:
                n_passtofail += 1

            if not in_position and not closed_today:
                if cur == GateState.PASS:
                    b_idx = max(0, int(np.searchsorted(
                        bar_starts_ns, td.timestamps[i], side="right")) - 1)
                    _q = q_tilde[:b_idx + 1]
                    if len(_q) >= N_HOLD and bool(np.all(_q[-N_HOLD:] >= Q_THRESHOLD)):
                        entry_t_sec = float(td.t_sec[i])
                        entry_price = float(td.prices[i])
                        in_position = True
                        closed_today = True
            elif in_position:
                if prev_state == GateState.PASS and cur != GateState.PASS:
                    in_position = False

            prev_state = cur

        t_event_offset_sec = t_event_sec - t_scanner_hit_sec
        t_warmup_end_offset_sec = (t_event_sec + EPG_WARMUP) - t_scanner_hit_sec
        entry_lag_sec = (entry_t_sec - t_event_sec) if entry_t_sec is not None else None
        t_entry_offset_sec = (entry_t_sec - t_scanner_hit_sec) if entry_t_sec is not None else None

        # Chart window (seconds from scanner)
        win_start_rel = t_event_offset_sec - 120.0
        win_end_rel = (t_entry_offset_sec + 300.0) if t_entry_offset_sec is not None else (t_event_offset_sec + 3900.0)

        # Per-tick data (windowed)
        tick_t_rel_all = td.t_sec - t_scanner_hit_sec
        tick_mask = (tick_t_rel_all >= win_start_rel) & (tick_t_rel_all <= win_end_rel)
        tick_t_rel = tick_t_rel_all[tick_mask].tolist()
        tick_E = E_out[tick_mask].tolist()
        tick_E_peak_threshold = np.array(e_peak_thresholds)[tick_mask].tolist()

        # State intervals (full run)
        state_intervals = _build_state_intervals(
            tick_t_rel_all.tolist(),
            [s.name for s in epg_states],
        )

        # 10s OHLC bars (windowed)
        b10s_starts, b10s_o, b10s_h, b10s_l, b10s_c = _build_10s_bars(
            td.timestamps, td.prices, start_ns,
        )
        if len(b10s_starts) > 0:
            b10s_t_rel_all = (b10s_starts.astype(np.float64) - t_scanner_hit_ns) / NS_PER_SECOND
            bm = (b10s_t_rel_all >= win_start_rel) & (b10s_t_rel_all <= win_end_rel)
            b10s_t = b10s_t_rel_all[bm].tolist()
            b10s_o_w = b10s_o[bm].tolist()
            b10s_h_w = b10s_h[bm].tolist()
            b10s_l_w = b10s_l[bm].tolist()
            b10s_c_w = b10s_c[bm].tolist()
        else:
            b10s_t = b10s_o_w = b10s_h_w = b10s_l_w = b10s_c_w = []

        # Q̃ trajectory (bar close times, windowed)
        n_bars_for_q = min(n_q_bars, n_bars)
        if n_bars_for_q > 0:
            bar_close_t_rel = (
                bar_starts_ns[:n_bars_for_q].astype(np.float64)
                + 60 * NS_PER_SECOND
                - t_scanner_hit_ns
            ) / NS_PER_SECOND
            qm = (bar_close_t_rel >= win_start_rel) & (bar_close_t_rel <= win_end_rel)
            qtilde_t_rel = bar_close_t_rel[qm].tolist()
            qtilde_vals = q_tilde[:n_bars_for_q][qm].tolist()
        else:
            qtilde_t_rel = []
            qtilde_vals = []

        # LULD halt fires (start of each detected halt window)
        luld_fires = []
        try:
            import pandas as _pd
            df = _pd.DataFrame(
                {"price": td.prices},
                index=_pd.to_datetime(td.timestamps, unit="ns"),
            )
            for hw in detect_luld_halts(df, price_col="price"):
                idx = int(np.searchsorted(td.timestamps, hw.start.value, side="left"))
                if idx < N:
                    luld_fires.append({
                        "t_rel": float(td.t_sec[idx]) - t_scanner_hit_sec,
                        "price": float(td.prices[idx]),
                    })
        except Exception:
            pass

        # First-3-consecutive vrect bounds (bar start/end in rel seconds)
        first_3c_vrect = None
        if first_3c_idx is not None and first_3c_idx + N_HOLD <= n_bars:
            f3_start = (float(bar_starts_ns[first_3c_idx]) - t_scanner_hit_ns) / NS_PER_SECOND
            f3_end = (
                float(bar_starts_ns[first_3c_idx + N_HOLD - 1])
                + 60 * NS_PER_SECOND
                - t_scanner_hit_ns
            ) / NS_PER_SECOND
            first_3c_vrect = (f3_start, f3_end)

        # Diamond marker at first_3consec bar start
        first_3c_diamond = None
        if first_3c_idx is not None and first_3c_offset is not None and first_3c_idx < n_q_bars:
            first_3c_diamond = {
                "t_rel": first_3c_offset,
                "q": float(q_tilde[first_3c_idx]),
            }

        return {
            **base,
            "status": "event",
            "has_trade": entry_t_sec is not None,
            "prev_close": float(prev_close),
            "t_event_offset_sec": round(t_event_offset_sec, 1),
            "t_warmup_end_offset_sec": round(t_warmup_end_offset_sec, 1),
            "first_q_offset_sec": round(first_q_offset, 1) if first_q_offset is not None else None,
            "first_3consec_offset_sec": round(first_3c_offset, 1) if first_3c_offset is not None else None,
            "t_entry_offset_sec": round(t_entry_offset_sec, 1) if t_entry_offset_sec is not None else None,
            "entry_lag_sec": round(entry_lag_sec, 1) if entry_lag_sec is not None else None,
            "gate_chatter_count": int(n_passtofail),
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
                "tick_E_peak_threshold": tick_E_peak_threshold,
                "qtilde_t_rel": qtilde_t_rel,
                "qtilde": qtilde_vals,
                "entry_price": entry_price,
                "luld_fires": luld_fires,
                "t_event_offset_sec": t_event_offset_sec,
                "t_warmup_end_offset_sec": t_warmup_end_offset_sec,
                "entry_lag_sec": entry_lag_sec,
                "first_3consec_offset_sec": first_3c_offset,
                "first_q_offset_sec": first_q_offset,
                "first_3consec_vrect": first_3c_vrect,
                "first_3consec_diamond": first_3c_diamond,
                "p_open": p_open,
                "p_close": p_close,
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

_STATE_FILL = {
    "INACTIVE": "rgba(200,200,200,0.25)",
    "WARMUP":   "rgba(255,179,71,0.30)",
    "PASS":     "rgba(100,200,100,0.28)",
    "FAIL":     "rgba(220,80,80,0.28)",
}
_STATE_SOLID = {
    "INACTIVE": "rgba(190,190,190,0.90)",
    "WARMUP":   "rgba(255,165,0,0.90)",
    "PASS":     "rgba(60,180,60,0.90)",
    "FAIL":     "rgba(210,60,60,0.90)",
}


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
    p_open_val = cd.get("p_open", P_OPEN)
    p_close_val = cd.get("p_close", P_CLOSE)

    win_start = cd["win_start_rel"]
    win_end = cd["win_end_rel"]
    state_intervals = cd["state_intervals"]
    t_event_rel = cd["t_event_offset_sec"]
    t_warmup_rel = cd["t_warmup_end_offset_sec"]
    entry_lag = cd["entry_lag_sec"]
    t_entry_rel = (t_event_rel + entry_lag) if entry_lag is not None else None
    entry_price = cd["entry_price"]
    luld_fires = cd["luld_fires"]
    first_3c_vrect = cd["first_3consec_vrect"]
    first_3c_diamond = cd["first_3consec_diamond"]

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.38, 0.22, 0.22, 0.18],
        subplot_titles=[
            "Price (10s OHLC)",
            "Gate Intensity  λ_V = E(t)",
            "Q̃ Trajectory",
            "EPG Gate State",
        ],
    )

    # Background shading panels 1 & 2
    # yref="y domain" spans full subplot height in paper coords — does not affect data range
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
                name="Price (10s)",
                increasing_line_color="#2ca02c",
                decreasing_line_color="#d62728",
                showlegend=False,
            ),
            row=1, col=1,
        )

    if t_entry_rel is not None and entry_price is not None:
        fig.add_trace(
            go.Scatter(
                x=[t_entry_rel], y=[entry_price],
                mode="markers",
                marker=dict(symbol="triangle-up", size=12, color="green"),
                name="Entry",
            ),
            row=1, col=1,
        )

    if luld_fires:
        fig.add_trace(
            go.Scatter(
                x=[f["t_rel"] for f in luld_fires],
                y=[f["price"] for f in luld_fires],
                mode="markers",
                marker=dict(symbol="x", size=10, color="orange"),
                name="LULD halt",
            ),
            row=1, col=1,
        )

    # Vertical lines (no row/col → spans all subplots)
    fig.add_vline(x=0.0, line_color="blue", line_width=1.8,
                  annotation_text="scanner", annotation_position="top right")
    fig.add_vline(x=t_event_rel, line_color="red", line_width=1.8,
                  annotation_text="T₀", annotation_position="top left")
    fig.add_vline(x=t_warmup_rel, line_color="red", line_width=1.2,
                  line_dash="dash", annotation_text="warmup end",
                  annotation_position="top left")
    if t_entry_rel is not None:
        fig.add_vline(x=t_entry_rel, line_color="green", line_width=1.8,
                      annotation_text="entry", annotation_position="top right")

    # Panel 2: E(t) + p×E_peak step line
    tick_t_rel = cd["tick_t_rel"]
    tick_E = cd["tick_E"]
    tick_E_thr = cd["tick_E_peak_threshold"]

    if tick_t_rel:
        fig.add_trace(
            go.Scatter(
                x=tick_t_rel, y=tick_E,
                mode="lines",
                line=dict(color="steelblue", width=1.3),
                name="E(t) = λ_V",
            ),
            row=2, col=1,
        )
        fig.add_trace(
            go.Scatter(
                x=tick_t_rel, y=tick_E_thr,
                mode="lines",
                line=dict(color="navy", width=1.0, dash="dot"),
                name=f"p×E_peak  (p={p_open_val})",
            ),
            row=2, col=1,
        )

    # Panel 3: Q̃
    qtilde_t = cd["qtilde_t_rel"]
    qtilde_v = cd["qtilde"]

    if qtilde_t:
        qv = np.array(qtilde_v)
        green_y = [float(v) if v >= Q_THRESHOLD else None for v in qv]
        red_y   = [float(v) if v < Q_THRESHOLD  else None for v in qv]

        if any(v is not None for v in green_y):
            fig.add_trace(
                go.Scatter(
                    x=qtilde_t, y=green_y,
                    mode="lines+markers",
                    line=dict(color="green", width=1.5),
                    marker=dict(size=4),
                    name="Q̃ ≥ 0.65",
                    connectgaps=False,
                ),
                row=3, col=1,
            )
        if any(v is not None for v in red_y):
            fig.add_trace(
                go.Scatter(
                    x=qtilde_t, y=red_y,
                    mode="lines+markers",
                    line=dict(color="red", width=1.5),
                    marker=dict(size=4),
                    name="Q̃ < 0.65",
                    connectgaps=False,
                ),
                row=3, col=1,
            )

        # Q̃ threshold dashed line
        fig.add_shape(
            type="line",
            x0=win_start, x1=win_end,
            y0=Q_THRESHOLD, y1=Q_THRESHOLD,
            line=dict(color="black", width=1.0, dash="dash"),
            row=3, col=1,
        )

        # First-3-consecutive vrect
        if first_3c_vrect:
            fig.add_shape(
                type="rect",
                x0=first_3c_vrect[0], x1=first_3c_vrect[1],
                y0=0, y1=1.05,
                row=3, col=1,
                fillcolor="rgba(144,238,144,0.40)",
                layer="below",
                line_width=0,
            )

        # Diamond at first_3consec bar start
        if first_3c_diamond:
            fig.add_trace(
                go.Scatter(
                    x=[first_3c_diamond["t_rel"]],
                    y=[first_3c_diamond["q"]],
                    mode="markers",
                    marker=dict(symbol="diamond", size=10, color="darkgreen"),
                    name="first 3-consec",
                ),
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

    # Dummy traces for Panel 4 legend
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
    h1_tag = "⚠ H1" if t_event_rel > 0 else "H1 ok"
    chatter = diag.get("gate_chatter_count", "?")
    lag_str = f"{entry_lag:.0f}s" if entry_lag is not None else "—"

    fig.update_layout(
        title=dict(
            text=(
                f"{ticker}  {date}  —  T₀ offset: {t_event_rel:+.0f}s ({h1_tag})  "
                f"entry_lag: {lag_str}  chatter: {chatter}"
            ),
            font=dict(size=13),
        ),
        height=900,
        hovermode="x unified",
        xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    fig.update_xaxes(range=[win_start, win_end])
    fig.update_xaxes(title_text="Seconds from scanner hit", row=4, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="E(t)", row=2, col=1)
    fig.update_yaxes(title_text="Q̃", range=[0, 1.05], row=3, col=1)
    fig.update_yaxes(title_text="State", range=[0, 1], showticklabels=False, row=4, col=1)

    chart_path = out_dir / f"{ticker}_{date}.html"
    fig.write_html(
        str(chart_path),
        include_plotlyjs="cdn",
        config={"responsive": True},
    )
    return chart_path


# ── Index HTML ────────────────────────────────────────────────────────────────

def generate_index(events: list[dict], chart_dir: Path,
                   p_open: float = P_OPEN, p_close: float = P_CLOSE) -> None:
    rows = []
    for ev in events:
        if ev.get("status") != "event" or not ev.get("has_trade"):
            continue
        chart_file = f"{ev['ticker']}_{ev['session_date']}.html"
        t0_offset = ev.get("t_event_offset_sec") or 0.0
        rows.append({
            "ticker": ev["ticker"],
            "date": ev["session_date"],
            "t_event_offset_sec": t0_offset,
            "t0_color": "red" if t0_offset > 0 else "green",
            "first_q_offset_sec": ev.get("first_q_offset_sec"),
            "first_3consec_offset_sec": ev.get("first_3consec_offset_sec"),
            "entry_lag_sec": ev.get("entry_lag_sec"),
            "gate_chatter_count": ev.get("gate_chatter_count"),
            "chart": chart_file if (chart_dir / chart_file).exists() else None,
        })

    # Default: first_3consec_offset_sec DESC
    rows.sort(
        key=lambda r: r["first_3consec_offset_sec"]
        if r["first_3consec_offset_sec"] is not None else -1e9,
        reverse=True,
    )

    def _fmt(v):
        if v is None:
            return "—"
        return f"{v:.1f}" if isinstance(v, float) else str(v)

    row_html = "\n".join(
        f'<tr>'
        f'<td>{r["ticker"]}</td>'
        f'<td>{r["date"]}</td>'
        f'<td style="color:{r["t0_color"]}">{_fmt(r["t_event_offset_sec"])}</td>'
        f'<td>{_fmt(r["first_q_offset_sec"])}</td>'
        f'<td>{_fmt(r["first_3consec_offset_sec"])}</td>'
        f'<td>{_fmt(r["entry_lag_sec"])}</td>'
        f'<td>{_fmt(r["gate_chatter_count"])}</td>'
        f'<td>{"<a href=" + chr(34) + r["chart"] + chr(34) + " target=_blank>chart</a>" if r["chart"] else "—"}</td>'
        f'</tr>'
        for r in rows
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>R1 Entry Lag Diagnostic (Redo)</title>
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
let _sortCol = 4, _sortAsc = false;
function sortTable(n) {{
  var t = document.getElementById("diag");
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
<h2>R1 Entry Lag Diagnostic (Redo) — p={p_open}/{p_close}</h2>
<p>
  Default sort: <b>first_3consec DESC</b> (slowest Q̃ qualification first).
  T₀ offset: <span style="color:red">red = H1 confirmed (anchor after scanner)</span>,
  <span style="color:green">green = H1 clear</span>.
  Click column headers to re-sort.
</p>
<table id="diag">
<thead><tr>
  <th onclick="sortTable(0)">Ticker</th>
  <th onclick="sortTable(1)">Date</th>
  <th onclick="sortTable(2)">T₀ offset (s)</th>
  <th onclick="sortTable(3)">first_q (s)</th>
  <th onclick="sortTable(4)">first_3consec (s)</th>
  <th onclick="sortTable(5)">entry_lag (s)</th>
  <th onclick="sortTable(6)">chatter</th>
  <th>Chart</th>
</tr></thead>
<tbody>
{row_html}
</tbody>
</table>
<p style="margin-top:16px; color:#666; font-size:12px">
  T₀ offset = t_event − t_scanner_hit.  Negative ≈ −30s expected (anchor fires before scanner).<br>
  first_3consec = seconds after scanner hit until first run of {N_HOLD} consecutive Q̃ ≥ {Q_THRESHOLD} bars.<br>
  entry_lag = seconds from T₀ to actual entry (EPG PASS &amp; Q̃ qualified).
</p>
</body>
</html>"""

    with open(chart_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    # Load traded events from T1 per_trade.json
    t1_path = RESULTS_DIR / "symmetric_p65" / "per_trade.json"
    if not t1_path.exists():
        print(f"ERROR: {t1_path} not found — run T1 first", file=sys.stderr)
        sys.exit(1)
    with open(t1_path) as f:
        t1_trades = json.load(f)
    traded_keys = {(tr["ticker"], tr["date"]) for tr in t1_trades}
    print(f"Loaded {len(traded_keys)} traded events from T1 per_trade.json")

    # Val event lookup
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

    args_list = []
    for ticker, date in sorted(traded_keys):
        ev = val_lookup.get((ticker, date))
        if ev is None:
            print(f"  WARNING: {ticker} {date} not in val events — skipping")
            continue
        fp = per_event_params.get((ticker, date), hawkes_median)
        args_list.append({
            "ticker": ticker,
            "date": date,
            "mom_pct": ev["mom_pct"],
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
        })

    print(f"Processing {len(args_list)} events (6 workers)...")
    raw_results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futures = {ex.submit(_collect_event_diag, a): a for a in args_list}
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

    print(f"Events: {len(event_results)} processable, {len(traded_results)} with trades")

    # Aggregates
    offsets     = [r["t_event_offset_sec"] for r in event_results if r.get("t_event_offset_sec") is not None]
    first_q_off = [r["first_q_offset_sec"] for r in event_results if r.get("first_q_offset_sec") is not None]
    first_3c_off = [r["first_3consec_offset_sec"] for r in event_results if r.get("first_3consec_offset_sec") is not None]
    entry_lags  = [r["entry_lag_sec"] for r in traded_results if r.get("entry_lag_sec") is not None]
    h1_count    = sum(1 for x in offsets if x > 0)

    def pct(arr, p):
        return round(float(np.percentile(arr, p)), 1) if arr else None

    aggregates = {
        "n_events": len(event_results),
        "n_traded": len(traded_results),
        "median_t_event_offset_sec": pct(offsets, 50),
        "p10_t_event_offset_sec": pct(offsets, 10),
        "p90_t_event_offset_sec": pct(offsets, 90),
        "pct_events_where_h1_confirmed": round(100 * h1_count / len(offsets), 1) if offsets else None,
        "median_first_qualifying_bar_offset_sec": pct(first_q_off, 50),
        "median_first_3consec_offset_sec": pct(first_3c_off, 50),
        "median_entry_lag_sec": pct(entry_lags, 50),
        "p90_entry_lag_sec": pct(entry_lags, 90),
    }

    # Write entry_lag_diagnosis.json (sorted by first_3consec DESC)
    diag_rows = [{k: v for k, v in r.items() if k != "_chart"} for r in event_results]
    diag_rows.sort(
        key=lambda r: r.get("first_3consec_offset_sec") or -1e9,
        reverse=True,
    )
    diag_path = RESULTS_DIR / "entry_lag_diagnosis.json"
    with open(diag_path, "w") as f:
        json.dump(
            {"events": diag_rows, "aggregates": aggregates},
            f, indent=2,
            default=lambda x: None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x,
        )
    print(f"Diagnosis written to {diag_path}")

    # Generate charts
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

    generate_index(event_results, CHART_DIR)
    print(f"Index written to {CHART_DIR}/index.html")
    print(f"Charts OK: {n_ok}/{len(traded_results)}")

    # Summary
    print("\n=== Entry Lag Diagnostic Aggregates ===")
    print(f"  n_events: {aggregates['n_events']}, n_traded: {aggregates['n_traded']}")
    print(f"  t_event_offset — median: {aggregates['median_t_event_offset_sec']}s  "
          f"p10: {aggregates['p10_t_event_offset_sec']}s  "
          f"p90: {aggregates['p90_t_event_offset_sec']}s")
    print(f"  H1 rate (anchor AFTER scanner): {aggregates['pct_events_where_h1_confirmed']}%")
    print(f"  median first_q_offset: {aggregates['median_first_qualifying_bar_offset_sec']}s")
    print(f"  median first_3consec_offset: {aggregates['median_first_3consec_offset_sec']}s")
    print(f"  entry_lag — median: {aggregates['median_entry_lag_sec']}s  "
          f"p90: {aggregates['p90_entry_lag_sec']}s")
    print("DIAGNOSTIC COMPLETE")


if __name__ == "__main__":
    main()
