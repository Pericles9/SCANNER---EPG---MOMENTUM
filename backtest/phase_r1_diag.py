#!/usr/bin/env python3
"""R1 T1 Diagnostic — entry lag analysis (H1 vs H2).

H1: EventAnchor fires late (after or well after scanner hit).
H2: Q̃ smoothing (rho_fast=0.90) delays 3 consecutive qualifying bars.

Outputs:
  results/phase_r1/entry_lag_diagnosis.json
  results/phase_r1/diagnostic_charts/<ticker>_<date>.png  (traded events only)
  results/phase_r1/diagnostic_charts/index.html
"""
from __future__ import annotations

import json
import math
import sys
import traceback as tb_module
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # non-interactive backend
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

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
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online, HawkesParams
from core.filters.rapid_entry import Q_THRESHOLD
from core.features.luld_halt_detection import detect_luld_halts

# Constants (from runner_rapid.py — kept in sync manually)
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000
REFIT_INTERVAL = 50
REFIT_WINDOW = 10000
BETA_FIXED = 0.1
HALT_GAP_THRESHOLD = 60.0

REPO = Path(__file__).resolve().parent.parent
RESULTS_DIR = BACKTEST / "results" / "phase_r1"
CHART_DIR = RESULTS_DIR / "diagnostic_charts"
CHART_DIR.mkdir(parents=True, exist_ok=True)

N_HOLD = 3
P_OPEN = 0.65
P_CLOSE = 0.65
SCANNER_THRESHOLD = 0.30  # 30% intraday change triggers scanner


# ── Hawkes replay (self-contained copy from runner_rapid.py) ──────────────────

def _build_halt_intervals(td) -> list:
    try:
        import pandas as _pd
        trades_df = _pd.DataFrame(
            {"price": td.prices},
            index=_pd.to_datetime(td.timestamps, unit="ns"),
        )
        halt_windows = detect_luld_halts(trades_df, price_col="price")
        if not halt_windows:
            return []
        t0_ns = int(td.timestamps[0])
        return [
            ((hw.start.value - t0_ns) / NS_PER_SECOND,
             (hw.end.value - t0_ns) / NS_PER_SECOND)
            for hw in halt_windows
        ]
    except Exception:
        return []


def _hawkes_replay_with_refit(
    t_sec, sides, rho, lambda_ref, init_params, rho_E,
    lam_buy_out, lam_sell_out, E_out, Edot_out, n_base_out,
    dv_arr=None, halt_intervals=None,
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
        _nb = (init_params["alpha_buy_self"] + init_params["alpha_sell_self"]) / BETA_FIXED
        n_base_out[:] = _nb
        return None

    params = fit_hawkes_forgetting(
        t_sec=t_sec[:cold_end], sides=sides[:cold_end],
        rho=rho, lambda_ref=lambda_ref, T=float(t_sec[cold_end - 1]),
        init_params=init_arr, n_restarts=5, beta_fixed=BETA_FIXED,
    )
    cold_start_params = params

    refit_points = list(range(cold_end + REFIT_INTERVAL, N + 1, REFIT_INTERVAL))
    if refit_points and refit_points[-1] < N:
        refit_points.append(N)
    elif not refit_points and N > cold_end:
        refit_points = [N]

    chunk_starts = [0, cold_end] + refit_points[:-1] if refit_points else [0, cold_end]
    chunk_ends = [cold_end] + refit_points if refit_points else [cold_end]
    if not refit_points:
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

        mu_total = params.mu_buy + params.mu_sell
        if mu_total < 1e-10:
            mu_total = 1e-10
        chunk_n_base = (params.alpha_buy_self + params.alpha_sell_self) / params.beta

        for i in range(c_start, c_end):
            n_base_out[i] = chunk_n_base
            if i == 0:
                lam_b = params.mu_buy
                lam_s = params.mu_sell
                lam_total = max(lam_b, 0.0) + max(lam_s, 0.0)
                E_val = lam_total / mu_total
                lam_buy_out[0] = max(lam_b, 0.0)
                lam_sell_out[0] = max(lam_s, 0.0)
                E_out[0] = E_val
                Edot_out[0] = 0.0
                if sides[0] == 1:
                    R_buy = 1.0
                else:
                    R_sell = 1.0
                E_prev = E_val
            else:
                dt = t_sec[i] - t_sec[i - 1]
                dt_eff = dt
                if _halt_ivs and dt_eff > HALT_GAP_THRESHOLD:
                    t_prev, t_curr = t_sec[i - 1], t_sec[i]
                    for h_s, h_e in _halt_ivs:
                        if t_prev < h_e and t_curr > h_s:
                            dt_eff = 1e-6
                            break
                if dt_eff > 0:
                    decay = np.exp(-params.beta * dt_eff)
                    R_buy *= decay
                    R_sell *= decay

                lam_b = max(0.0, params.mu_buy + params.alpha_buy_self * R_buy)
                lam_s = max(0.0, params.mu_sell + params.alpha_sell_self * R_sell)
                lam_total = lam_b + lam_s
                E_val = lam_total / mu_total
                dt_capped = max(min(dt_eff, 1.0), 1e-12)
                raw_slope = (E_val - E_prev) / dt_capped
                Edot_ema = rho_E * Edot_ema + (1.0 - rho_E) * raw_slope

                lam_buy_out[i] = lam_b
                lam_sell_out[i] = lam_s
                E_out[i] = E_val
                Edot_out[i] = Edot_ema

                if sides[i] == 1:
                    R_buy += 1.0
                else:
                    R_sell += 1.0
                E_prev = E_val

    return cold_start_params


# ── Worker ────────────────────────────────────────────────────────────────────

def _collect_event_diag(args: dict) -> dict:
    """Full replay for one event; returns timing diagnostic + chart data."""
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]

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

        # ── Scanner hit time ──────────────────────────────────────────────
        # First trade where intraday_pct >= 30% (proxy for scanner trigger)
        t_scanner_hit_sec = None
        t_scanner_hit_ns = None
        for i in range(N):
            if (td.prices[i] - prev_close) / prev_close >= SCANNER_THRESHOLD:
                t_scanner_hit_sec = float(td.t_sec[i])
                t_scanner_hit_ns = int(td.timestamps[i])
                break
        if t_scanner_hit_sec is None:
            # Event never crossed 30% in this window — use first trade as fallback
            t_scanner_hit_sec = 0.0
            t_scanner_hit_ns = int(td.timestamps[0])

        # ── Setup filter + bar starts ─────────────────────────────────────
        sf = run_setup_filter(
            timestamps=td.timestamps,
            prices=td.prices,
            sizes=td.sizes,
            session_start_ns=start_ns,
            session_end_ns=end_ns,
        )
        opens, highs, lows, closes, vols, dvols, bar_starts_ns = _build_1min_bars(
            td.timestamps, td.prices, td.sizes.astype(np.int64), start_ns, end_ns
        )
        n_bars = len(bar_starts_ns)

        # ── Lee-Ready sides ───────────────────────────────────────────────
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

        # ── Hawkes replay ─────────────────────────────────────────────────
        halt_intervals = _build_halt_intervals(td)
        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)
        dv_arr = td.prices.astype(np.float64) * td.sizes.astype(np.float64)

        global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = global_lambda_ref if (math.isnan(per_event_lref) or per_event_lref <= 0) else per_event_lref

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
            dv_arr=dv_arr,
            halt_intervals=halt_intervals or None,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # ── EPG ───────────────────────────────────────────────────────────
        global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)

        gate = ParticipationGate(
            half_life_seconds=EPG_TAU,
            peak_threshold_p=P_OPEN,
            warmup_seconds=EPG_WARMUP,
            p_open=P_OPEN,
            p_close=P_CLOSE,
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

        # ── Q̃ qualifying bar analysis ─────────────────────────────────────
        q_tilde = sf.q_tilde  # shape (n_bars,) — one value per 1-min bar
        n_q_bars = len(q_tilde)

        # First qualifying bar (Q̃ >= threshold)
        first_q_bar_idx = None
        for i in range(n_q_bars):
            if q_tilde[i] >= Q_THRESHOLD:
                first_q_bar_idx = i
                break

        # First run of N_HOLD consecutive qualifying bars
        first_3consec_idx = None
        consec = 0
        for i in range(n_q_bars):
            if q_tilde[i] >= Q_THRESHOLD:
                consec += 1
                if consec >= N_HOLD:
                    first_3consec_idx = i - (N_HOLD - 1)
                    break
            else:
                consec = 0

        # Convert bar indices to t_sec (seconds from first trade)
        bar_t_sec = (bar_starts_ns - td.timestamps[0]).astype(np.float64) / NS_PER_SECOND

        def bar_offset(idx):
            if idx is None or idx >= len(bar_t_sec):
                return None
            return float(bar_t_sec[idx] - t_scanner_hit_sec)

        first_q_offset = bar_offset(first_q_bar_idx)
        first_3consec_offset = bar_offset(first_3consec_idx)

        # ── State-machine replay (to find entry) ──────────────────────────
        prev_state = GateState.INACTIVE
        in_position = False
        closed_today = False
        entry_t_sec = None
        n_passtofail = 0
        n_entry_eligible_blocks = 0

        for i in range(N):
            cur = epg_states[i]
            if prev_state == GateState.PASS and cur != GateState.PASS:
                n_passtofail += 1

            if not in_position and not closed_today:
                if cur == GateState.PASS:
                    _bar_idx = max(0, int(np.searchsorted(
                        bar_starts_ns, td.timestamps[i], side="right")) - 1)
                    _q = sf.q_tilde[:_bar_idx + 1]
                    if len(_q) < N_HOLD or not bool(np.all(_q[-N_HOLD:] >= Q_THRESHOLD)):
                        n_entry_eligible_blocks += 1
                    else:
                        entry_t_sec = float(td.t_sec[i])
                        in_position = True
                        closed_today = True
            elif in_position:
                if prev_state == GateState.PASS and cur != GateState.PASS:
                    in_position = False

            prev_state = cur

        t_entry_offset_sec = (float(entry_t_sec) - t_scanner_hit_sec) if entry_t_sec is not None else None
        t_event_offset_vs_scanner_sec = t_event_sec - t_scanner_hit_sec
        t_warmup_end_offset_sec = (t_event_sec + EPG_WARMUP) - t_scanner_hit_sec

        # ── Per-bar gate state for charting ──────────────────────────────
        # Dominant gate state per 1-min bar
        bar_gate_states = ["INACTIVE"] * n_bars
        for i in range(N):
            b_idx = int(np.searchsorted(bar_starts_ns, td.timestamps[i], side="right")) - 1
            if 0 <= b_idx < n_bars:
                s = epg_states[i].name
                # Prefer PASS > WARMUP > FAIL > INACTIVE
                cur_s = bar_gate_states[b_idx]
                priority = {"PASS": 4, "WARMUP": 3, "FAIL": 2, "INACTIVE": 1}
                if priority.get(s, 0) > priority.get(cur_s, 0):
                    bar_gate_states[b_idx] = s

        # ── Return payload ────────────────────────────────────────────────
        has_trade = entry_t_sec is not None

        return {
            **base,
            "status": "event",
            "has_trade": has_trade,
            "prev_close": float(prev_close),
            # Diagnostic timing fields
            "t_event_offset_vs_scanner_sec": round(t_event_offset_vs_scanner_sec, 1),
            "t_warmup_end_offset_sec": round(t_warmup_end_offset_sec, 1),
            "first_qualifying_bar_offset_sec": round(first_q_offset, 1) if first_q_offset is not None else None,
            "first_3consec_qualifying_bar_offset_sec": round(first_3consec_offset, 1) if first_3consec_offset is not None else None,
            "t_entry_offset_sec": round(t_entry_offset_sec, 1) if t_entry_offset_sec is not None else None,
            "entry_lag_sec": round(float(entry_t_sec) - t_event_sec, 1) if entry_t_sec is not None else None,
            "gate_chatter_count": int(n_passtofail),
            "n_entry_eligible_blocks": int(n_entry_eligible_blocks),
            # Chart data (kept in memory for chart generation — NOT written to JSON directly)
            "_chart": {
                "bar_ts_ns": bar_starts_ns.tolist(),
                "first_trade_ns": int(td.timestamps[0]),
                "q_tilde": q_tilde.tolist() if len(q_tilde) > 0 else [],
                "bar_opens": opens.tolist(),
                "bar_highs": highs.tolist(),
                "bar_lows": lows.tolist(),
                "bar_closes": closes.tolist(),
                "bar_gate_states": bar_gate_states,
                "t_event_sec": t_event_sec,
                "t_scanner_hit_sec": t_scanner_hit_sec,
                "t_scanner_hit_ns": int(t_scanner_hit_ns) if t_scanner_hit_ns else int(td.timestamps[0]),
                "entry_t_sec": entry_t_sec,
                "first_q_bar_idx": first_q_bar_idx,
                "first_3consec_idx": first_3consec_idx,
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

STATE_COLORS = {
    "INACTIVE": "#f0f0f0",
    "WARMUP":   "#fff3cd",
    "PASS":     "#d4edda",
    "FAIL":     "#f8d7da",
}


def _ns_to_dt(ns_val: int) -> pd.Timestamp:
    return pd.Timestamp(ns_val, unit="ns", tz="America/New_York")


def generate_chart(diag: dict, out_dir: Path) -> Path | None:
    """Generate 2-panel diagnostic chart for one event. Returns output path."""
    if not diag.get("has_trade"):
        return None

    cd = diag["_chart"]
    ticker = diag["ticker"]
    date = diag["session_date"]
    t_event_offset = diag["t_event_offset_vs_scanner_sec"]

    bar_ts_ns = np.array(cd["bar_ts_ns"], dtype=np.int64)
    first_trade_ns = cd["first_trade_ns"]
    q_tilde = np.array(cd["q_tilde"])
    bar_opens = np.array(cd["bar_opens"])
    bar_highs = np.array(cd["bar_highs"])
    bar_lows = np.array(cd["bar_lows"])
    bar_closes = np.array(cd["bar_closes"])
    bar_states = cd["bar_gate_states"]
    n_bars = len(bar_ts_ns)

    t_event_sec = cd["t_event_sec"]
    t_scanner_hit_sec = cd["t_scanner_hit_sec"]
    t_scanner_hit_ns = cd["t_scanner_hit_ns"]
    entry_t_sec = cd["entry_t_sec"]
    first_q_bar_idx = cd["first_q_bar_idx"]
    first_3consec_idx = cd["first_3consec_idx"]

    # Absolute timestamps from t_sec offsets (from first trade)
    t_event_ns = first_trade_ns + int(t_event_sec * NS_PER_SECOND)
    t_warmup_end_ns = t_event_ns + int(EPG_WARMUP * NS_PER_SECOND)
    t_entry_ns = first_trade_ns + int(entry_t_sec * NS_PER_SECOND) if entry_t_sec is not None else None

    # Chart window: scanner - 5min to entry + 10min
    win_start_ns = t_scanner_hit_ns - 5 * 60 * NS_PER_SECOND
    win_end_ns = (t_entry_ns + 10 * 60 * NS_PER_SECOND) if t_entry_ns else (t_scanner_hit_ns + 90 * 60 * NS_PER_SECOND)

    # Filter bars to window
    mask = (bar_ts_ns >= win_start_ns) & (bar_ts_ns <= win_end_ns)
    if not mask.any():
        return None

    b_ts = bar_ts_ns[mask]
    b_states = [bar_states[i] for i, m in enumerate(mask) if m]
    b_opens = bar_opens[mask]
    b_highs = bar_highs[mask]
    b_lows = bar_lows[mask]
    b_closes = bar_closes[mask]

    # Q̃ bars (aligned to the same bar indices)
    n_q = len(q_tilde)
    bar_idx_in_window = [i for i, m in enumerate(mask) if m]
    b_q = np.array([q_tilde[i] if i < n_q else np.nan for i in bar_idx_in_window])

    # Bar width in seconds (58s to leave a gap)
    BAR_WIDTH_SEC = 58

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 8), sharex=True,
                                    gridspec_kw={"height_ratios": [3, 1.2]})

    h1_warn = " ⚠️ H1" if t_event_offset > 0 else ""
    fig.suptitle(f"{ticker} {date}  —  T-event offset vs scanner: "
                 f"{t_event_offset:+.0f}s{h1_warn}", fontsize=12, fontweight="bold")

    # ── Panel 1: OHLC + shading ───────────────────────────────────────────
    for i, (ts, state) in enumerate(zip(b_ts, b_states)):
        x0 = ts / NS_PER_SECOND
        x1 = x0 + 60
        color = STATE_COLORS.get(state, "#ffffff")
        ax1.axvspan(x0, x1, color=color, alpha=0.7, linewidth=0)

    # Candlestick bars
    for i, (ts, o, h, l, c) in enumerate(zip(b_ts, b_opens, b_highs, b_lows, b_closes)):
        x = ts / NS_PER_SECOND + 30  # center of bar
        color = "#2ca02c" if c >= o else "#d62728"
        ax1.plot([x, x], [l, h], color=color, linewidth=0.8)
        ax1.bar(x, abs(c - o), bottom=min(o, c), width=BAR_WIDTH_SEC,
                color=color, alpha=0.85, linewidth=0)

    # Vertical markers (in seconds-from-epoch, same scale as bar_ts_ns / NS_PER_SECOND)
    ax1.axvline(t_scanner_hit_ns / NS_PER_SECOND, color="#1f77b4", linewidth=1.8, label="scanner")
    ax1.axvline(t_event_ns / NS_PER_SECOND, color="#d62728", linewidth=1.8, label="T-event")
    ax1.axvline(t_warmup_end_ns / NS_PER_SECOND, color="#d62728", linewidth=1.2,
                linestyle="--", label="warmup end")
    if t_entry_ns:
        ax1.axvline(t_entry_ns / NS_PER_SECOND, color="#2ca02c", linewidth=1.8, label="entry")

    # Format x-axis as time-of-day (minutes offset from window start)
    x_min = win_start_ns / NS_PER_SECOND
    x_max = win_end_ns / NS_PER_SECOND
    ax1.set_xlim(x_min, x_max)

    # Custom x tick formatter: show as ET time HH:MM
    def _fmt_x(x_sec, _pos=None):
        try:
            return _ns_to_dt(int(x_sec * NS_PER_SECOND)).strftime("%H:%M")
        except Exception:
            return ""

    import matplotlib.ticker as ticker_mod
    ax1.xaxis.set_major_formatter(ticker_mod.FuncFormatter(_fmt_x))
    ax1.xaxis.set_major_locator(ticker_mod.MultipleLocator(300))  # every 5 min

    ax1.set_ylabel("Price")
    ax1.legend(loc="upper right", fontsize=8)

    # Legend for shading
    patches = [mpatches.Patch(color=v, alpha=0.7, label=k) for k, v in STATE_COLORS.items()]
    ax1.legend(handles=patches + ax1.get_lines()[:4], loc="upper left",
               fontsize=7, ncol=4)

    # ── Panel 2: Q̃ ────────────────────────────────────────────────────────
    q_bar_centers = b_ts / NS_PER_SECOND + 30
    bar_colors = ["#2ca02c" if q >= Q_THRESHOLD else "#d62728"
                  for q in b_q]
    ax2.bar(q_bar_centers, b_q, width=BAR_WIDTH_SEC,
            color=bar_colors, alpha=0.75, linewidth=0)
    ax2.axhline(Q_THRESHOLD, color="black", linewidth=1.2, linestyle="--",
                label=f"Q̃ = {Q_THRESHOLD}")

    # Shade first 3-consecutive qualifying region
    if first_3consec_idx is not None and first_3consec_idx < n_bars:
        end_idx = min(first_3consec_idx + N_HOLD, n_bars)
        shade_start = bar_ts_ns[first_3consec_idx] / NS_PER_SECOND
        shade_end = bar_ts_ns[end_idx - 1] / NS_PER_SECOND + 60
        ax2.axvspan(shade_start, shade_end, color="#d4edda", alpha=0.6, label="first 3-consec")

    # Mark entry eligible point
    if entry_t_sec is not None:
        ax2.axvline(first_trade_ns / NS_PER_SECOND + entry_t_sec,
                    color="#2ca02c", linewidth=1.8, linestyle=":", label="entry accepted")

    ax2.set_xlim(x_min, x_max)
    ax2.set_ylim(0, 1.05)
    ax2.set_ylabel("Q̃")
    ax2.set_xlabel("Time (ET)")
    ax2.legend(loc="upper right", fontsize=7)
    ax2.xaxis.set_major_formatter(ticker_mod.FuncFormatter(_fmt_x))
    ax2.xaxis.set_major_locator(ticker_mod.MultipleLocator(300))

    plt.tight_layout(rect=[0, 0, 1, 0.95])

    chart_name = f"{ticker}_{date}.png"
    chart_path = out_dir / chart_name
    plt.savefig(str(chart_path), dpi=130, bbox_inches="tight")
    plt.close(fig)
    return chart_path


# ── Index HTML ────────────────────────────────────────────────────────────────

def generate_index(events: list[dict], chart_dir: Path) -> None:
    rows = []
    for ev in events:
        if ev.get("status") != "event" or not ev.get("has_trade"):
            continue
        chart_file = f"{ev['ticker']}_{ev['session_date']}.png"
        chart_exists = (chart_dir / chart_file).exists()
        h1 = "⚠️" if (ev.get("t_event_offset_vs_scanner_sec") or 0) > 0 else ""
        rows.append({
            "ticker": ev["ticker"],
            "date": ev["session_date"],
            "t_event_offset": ev.get("t_event_offset_vs_scanner_sec"),
            "first_q_offset": ev.get("first_qualifying_bar_offset_sec"),
            "entry_lag": ev.get("entry_lag_sec"),
            "chatter": ev.get("gate_chatter_count"),
            "chart": chart_file if chart_exists else None,
            "h1": h1,
        })

    rows.sort(key=lambda r: r["t_event_offset"] if r["t_event_offset"] is not None else 0, reverse=True)

    def _fmt(v):
        if v is None:
            return "—"
        if isinstance(v, float):
            return f"{v:.1f}"
        return str(v)

    row_html = "\n".join(
        f'<tr>'
        f'<td>{r["h1"]}{r["ticker"]}</td>'
        f'<td>{r["date"]}</td>'
        f'<td>{_fmt(r["t_event_offset"])}</td>'
        f'<td>{_fmt(r["first_q_offset"])}</td>'
        f'<td>{_fmt(r["entry_lag"])}</td>'
        f'<td>{_fmt(r["chatter"])}</td>'
        f'<td>{"<a href=" + chr(34) + r["chart"] + chr(34) + ">chart</a>" if r["chart"] else "—"}</td>'
        f'</tr>'
        for r in rows
    )

    html = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>R1 T1 Entry Lag Diagnostic</title>
<style>
body {{ font-family: monospace; font-size: 13px; padding: 16px; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ccc; padding: 4px 8px; text-align: right; }}
th {{ background: #eee; cursor: pointer; }}
td:first-child, td:nth-child(2) {{ text-align: left; }}
tr:hover {{ background: #f9f9f9; }}
</style>
<script>
function sortTable(n) {{
  var t = document.getElementById("diag");
  var rows = Array.from(t.rows).slice(1);
  var asc = t.dataset.sort == n;
  rows.sort((a, b) => {{
    var av = a.cells[n].innerText, bv = b.cells[n].innerText;
    return asc ? av.localeCompare(bv, undefined, {{numeric:true}}) : bv.localeCompare(av, undefined, {{numeric:true}});
  }});
  rows.forEach(r => t.appendChild(r));
  t.dataset.sort = asc ? "" : n;
}}
</script>
</head>
<body>
<h2>R1 T1 Entry Lag Diagnostic — p=0.65/0.65</h2>
<p>Sorted by t_event_offset_vs_scanner_sec descending (worst H1 cases first). Click column headers to re-sort.</p>
<table id="diag" data-sort="">
<tr>
<th onclick="sortTable(0)">Ticker</th>
<th onclick="sortTable(1)">Date</th>
<th onclick="sortTable(2)">t_event_offset (s)</th>
<th onclick="sortTable(3)">first_q_bar_offset (s)</th>
<th onclick="sortTable(4)">entry_lag (s)</th>
<th onclick="sortTable(5)">chatter</th>
<th>Chart</th>
</tr>
{row_html}
</table>
<p style="margin-top:16px; color:#666">
t_event_offset &lt; 0: anchor fires BEFORE scanner (expected ~-30s, H1 clear).<br>
t_event_offset &gt; 0: anchor fires AFTER scanner (H1 contributor).<br>
first_q_bar_offset: seconds after scanner hit until Q̃ first crosses 0.65 (H2 diagnostic).
</p>
</body>
</html>"""

    with open(chart_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    import random

    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    all_events = list_events(min_mom=50.0, require_date=True)
    events = [e for e in all_events if val_start <= e["date"] < test_start]

    # Same stratified sample as T1 p=0.65 run
    rng = random.Random(42)
    n_sample = 100
    by_year: dict[str, list] = {}
    for e in events:
        by_year.setdefault(e["date"][:4], []).append(e)
    year_counts = {y: len(evs) for y, evs in by_year.items()}
    total = sum(year_counts.values())
    alloc = {y: int(n_sample * cnt / total) for y, cnt in year_counts.items()}
    remainder = n_sample - sum(alloc.values())
    for y in sorted(year_counts, key=year_counts.get, reverse=True):
        if remainder <= 0:
            break
        alloc[y] += 1
        remainder -= 1
    sampled = []
    for y in sorted(by_year):
        n_y = min(alloc[y], len(by_year[y]))
        sampled.extend(rng.sample(by_year[y], n_y))
    events = sorted(sampled, key=lambda e: (e["date"], e["ticker"]))
    print(f"Loaded {len(events)} events (stratified seed=42)")

    repo_root = BACKTEST.parent
    phase_a_path = repo_root / "results" / "phase_a" / "production_fit_results.json"
    per_event_params = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            phase_a_results = json.load(f)
        for r in phase_a_results:
            if r.get("status") == "success" and "final_params" in r:
                per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    args_list = []
    for ev in events:
        key = (ev["ticker"], ev["date"])
        fp = per_event_params.get(key, hawkes_median)
        args_list.append({
            "ticker": ev["ticker"],
            "date": ev["date"],
            "mom_pct": ev["mom_pct"],
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
        })

    print("Collecting diagnostic data (6 workers)...")
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
    print(f"Events: {len(event_results)} processable, {len(traded_results)} with trades")

    # ── Aggregate stats ────────────────────────────────────────────────────
    offsets = [r["t_event_offset_vs_scanner_sec"] for r in event_results
               if r.get("t_event_offset_vs_scanner_sec") is not None]
    first_q_offsets = [r["first_qualifying_bar_offset_sec"] for r in event_results
                       if r.get("first_qualifying_bar_offset_sec") is not None]
    first_3c_offsets = [r["first_3consec_qualifying_bar_offset_sec"] for r in event_results
                        if r.get("first_3consec_qualifying_bar_offset_sec") is not None]
    entry_lags = [r["entry_lag_sec"] for r in traded_results
                  if r.get("entry_lag_sec") is not None]
    h1_count = sum(1 for x in offsets if x > 0)

    def pct(arr, p):
        return round(float(np.percentile(arr, p)), 1) if arr else None

    aggregates = {
        "n_events": len(event_results),
        "n_traded": len(traded_results),
        "t_event_offset_vs_scanner_median_sec": pct(offsets, 50),
        "t_event_offset_vs_scanner_p90_sec": pct(offsets, 90),
        "h1_rate_pct": round(100 * h1_count / len(offsets), 1) if offsets else None,
        "first_qualifying_bar_offset_median_sec": pct(first_q_offsets, 50),
        "first_3consec_qualifying_bar_offset_median_sec": pct(first_3c_offsets, 50),
        "entry_lag_median_sec": pct(entry_lags, 50),
        "entry_lag_p90_sec": pct(entry_lags, 90),
    }

    # ── Build diagnosis JSON (strip chart data) ────────────────────────────
    diag_rows = []
    for r in event_results:
        row = {k: v for k, v in r.items() if k != "_chart"}
        diag_rows.append(row)
    diag_rows.sort(key=lambda r: r.get("t_event_offset_vs_scanner_sec") or 0, reverse=True)

    output = {"events": diag_rows, "aggregates": aggregates}
    diag_path = RESULTS_DIR / "entry_lag_diagnosis.json"
    with open(diag_path, "w") as f:
        json.dump(output, f, indent=2, default=lambda x: None if isinstance(x, float) and (math.isnan(x) or math.isinf(x)) else x)
    print(f"Diagnosis written to {diag_path}")

    # ── Generate charts ────────────────────────────────────────────────────
    print(f"Generating {len(traded_results)} charts...")
    for i, r in enumerate(traded_results):
        try:
            chart_path = generate_chart(r, CHART_DIR)
            if (i + 1) % 10 == 0:
                print(f"  {i+1}/{len(traded_results)} charts generated")
        except Exception as e:
            print(f"  Chart failed {r['ticker']} {r['session_date']}: {e}")

    generate_index(event_results, CHART_DIR)
    print(f"Index written to {CHART_DIR}/index.html")

    # ── Print aggregate summary ────────────────────────────────────────────
    print("\n=== Entry Lag Diagnostic Aggregates ===")
    print(f"  n_events: {aggregates['n_events']}, n_traded: {aggregates['n_traded']}")
    print(f"  t_event_offset vs scanner — median: {aggregates['t_event_offset_vs_scanner_median_sec']}s, "
          f"p90: {aggregates['t_event_offset_vs_scanner_p90_sec']}s")
    print(f"  H1 rate (anchor AFTER scanner): {aggregates['h1_rate_pct']}%")
    print(f"  first_qualifying_bar_offset median: {aggregates['first_qualifying_bar_offset_median_sec']}s")
    print(f"  first_3consec_qualifying_bar_offset median: {aggregates['first_3consec_qualifying_bar_offset_median_sec']}s")
    print(f"  entry_lag median: {aggregates['entry_lag_median_sec']}s, p90: {aggregates['entry_lag_p90_sec']}s")


if __name__ == "__main__":
    main()
