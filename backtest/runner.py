#!/usr/bin/env python3
"""
Phase U — Screening + EXIT_D + LULD Backtest Runner
=====================================================
Extends the Phase S screening-only runner with:
  1. Gap gate fix: queued wait-and-enter instead of hard block.
  2. EXIT_D: intensity imbalance timer exit (theta, tau_min from config).
  3. LULD proximity exit: Tier 2 bands, 2% proximity threshold, RTH only.

Exit priority per tick (first wins): EXIT_D > LULD > EPG window close.

Writes to: results/backtest/  (or --results-dir)
Reads from: specified split only (default: val). Test split is locked.

Config: config/strategy.json  (or --config)
"""
from __future__ import annotations

import argparse
import json
import logging
import math
import os
import sys
import tempfile
import time
import traceback as tb_module
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

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
from backtest.setup_filter import run_setup_filter
from core.exits.luld_proximity import LuldProximityExit, ProximityState
from core.exits.reentry import ReentrySignal
from core.hawkes.engine import hawkes_replay_fixed_beta
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online, HawkesParams


# ── Constants ──────────────────────────────────────────────────────────

PHASE_NAME = "scanner_epg_momentum"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "backtest"
LOG_DIR = Path(__file__).resolve().parent / "logs"

EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0

DEFAULT_GAP_THRESHOLD = 0.30

RTH_START_SEC = 19800.0   # 9:30 AM ET = 5.5h from 4 AM session start
RTH_END_SEC   = 43200.0   # 4:00 PM ET = 12h from 4 AM session start

# Online refit parameters
REFIT_INTERVAL = 50
COLD_START_SIZE = 1000
REFIT_WINDOW = 10000
BETA_FIXED = 0.1


# ── Utilities ──────────────────────────────────────────────────────────

def write_json_atomic(data, output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=output_path.parent, suffix=".tmp", delete=False,
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp_path = Path(f.name)
    os.replace(str(tmp_path), str(output_path))


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


# ── Logging ────────────────────────────────────────────────────────────

def setup_logging() -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"{PHASE_NAME}_{timestamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(name)s | pid=%(process)d | %(message)s",
        handlers=[logging.FileHandler(log_path), logging.StreamHandler()],
    )
    logger = logging.getLogger(PHASE_NAME)
    logger.info(f"Logging to {log_path}")
    return logger


log = setup_logging()


# ── Test split assertion ───────────────────────────────────────────────

def assert_split_valid(event_dates: list[str], split: str, boundary: dict) -> None:
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    if split == "val":
        violations = [d for d in event_dates if d < val_start or d >= test_start]
    elif split == "train":
        violations = [d for d in event_dates if d >= val_start]
    elif split == "trainval":
        violations = [d for d in event_dates if d >= test_start]
    elif split == "test":
        # T7 unlock: test split runs exactly once with Phase F config. No iteration.
        violations = [d for d in event_dates if d < test_start]
    else:
        violations = []
    if violations:
        raise ValueError(
            f"SPLIT VIOLATION: {len(violations)} events outside {split}. "
            f"First: {violations[0]}"
        )


# ── Session bucket ─────────────────────────────────────────────────────

def session_bucket(time_of_day_sec: float) -> str:
    if time_of_day_sec < RTH_START_SEC:
        return "pre_market"
    if time_of_day_sec < RTH_END_SEC:
        return "regular_hours"
    return "post_market"


# ── Hawkes replay with online refitting ───────────────────────────────

def _hawkes_replay_with_refit(
    t_sec: np.ndarray,
    sides: np.ndarray,
    rho: float,
    lambda_ref: float,
    init_params: dict,
    rho_E: float,
    lam_buy_out: np.ndarray,
    lam_sell_out: np.ndarray,
    E_out: np.ndarray,
    Edot_out: np.ndarray,
    n_base_out: np.ndarray,
    dv_arr=None,
    mu_buy_out=None,
    mu_sell_out=None,
    dbar_out=None,
    cold_start_size: int = COLD_START_SIZE,
    refit_interval: int = REFIT_INTERVAL,
    window_size: int = REFIT_WINDOW,
    beta_fixed: float = BETA_FIXED,
    halt_intervals=None,
    halt_gap_threshold: float = 60.0,
) -> "HawkesParams | None":
    """Replay Hawkes with online MLE refitting every refit_interval trades.

    halt_intervals: list of (start_sec, end_sec) in the same frame as t_sec
        (seconds since first trade). Gaps > halt_gap_threshold that overlap any
        interval get dt_eff = 1e-6, preventing Hawkes decay across a halt.
        None (default) is 100% identical to the original implementation.
    """
    N = len(t_sec)
    if N == 0:
        return None

    _halt_ivs: list = halt_intervals or []

    cold_end = min(cold_start_size, N)
    if cold_end < 100:
        hawkes_replay_fixed_beta(
            t_sec, sides,
            init_params["alpha_buy_self"], 0.0,
            init_params["alpha_sell_self"], 0.0,
            init_params["mu_buy"], init_params["mu_sell"],
            init_params["beta"], rho_E,
            lam_buy_out, lam_sell_out, E_out, Edot_out,
        )
        _nb = (init_params["alpha_buy_self"] + init_params["alpha_sell_self"]) / beta_fixed
        n_base_out[:] = _nb
        if mu_buy_out is not None:
            mu_buy_out[:] = init_params["mu_buy"]
        if mu_sell_out is not None:
            mu_sell_out[:] = init_params["mu_sell"]
        if dbar_out is not None and dv_arr is not None and N > 0:
            dbar_out[:] = float(np.mean(dv_arr))
        return None

    init_arr = np.array([
        init_params["alpha_buy_self"],
        init_params["alpha_sell_self"],
        init_params["mu_buy"], init_params["mu_sell"],
    ])

    params = fit_hawkes_forgetting(
        t_sec=t_sec[:cold_end],
        sides=sides[:cold_end],
        rho=rho,
        lambda_ref=lambda_ref,
        T=float(t_sec[cold_end - 1]),
        init_params=init_arr,
        n_restarts=5,
        beta_fixed=beta_fixed,
    )
    cold_start_params = params

    refit_points = list(range(cold_end + refit_interval, N + 1, refit_interval))
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
            w_start = max(0, c_end - window_size)
            params = fit_online(
                t_sec=t_sec[w_start:c_end],
                sides=sides[w_start:c_end],
                rho=rho,
                lambda_ref=lambda_ref,
                prev_params=params,
                T=float(t_sec[c_end - 1]),
                n_restarts=1,
                beta_fixed=beta_fixed,
            )

        mu_total = params.mu_buy + params.mu_sell
        if mu_total < 1e-10:
            mu_total = 1e-10
        chunk_n_base = (params.alpha_buy_self + params.alpha_sell_self) / params.beta

        if mu_buy_out is not None:
            mu_buy_out[c_start:c_end] = params.mu_buy
        if mu_sell_out is not None:
            mu_sell_out[c_start:c_end] = params.mu_sell
        if dbar_out is not None and dv_arr is not None:
            if chunk_idx == 0:
                _dw = dv_arr[:c_end]
            else:
                _dw = dv_arr[max(0, c_end - window_size):c_end]
            dbar_out[c_start:c_end] = float(np.mean(_dw)) if len(_dw) > 0 else 0.0

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
                if _halt_ivs and dt_eff > halt_gap_threshold:
                    t_prev, t_curr = t_sec[i - 1], t_sec[i]
                    for h_s, h_e in _halt_ivs:
                        if t_prev < h_e and t_curr > h_s:
                            dt_eff = 1e-6
                            break
                if dt_eff > 0:
                    decay = np.exp(-params.beta * dt_eff)
                    R_buy *= decay
                    R_sell *= decay

                lam_b = params.mu_buy + params.alpha_buy_self * R_buy
                lam_s = params.mu_sell + params.alpha_sell_self * R_sell
                if lam_b < 0.0:
                    lam_b = 0.0
                if lam_s < 0.0:
                    lam_s = 0.0

                lam_total = lam_b + lam_s
                E_val = lam_total / mu_total

                dt_capped = min(dt_eff, 1.0)
                if dt_capped < 1e-12:
                    dt_capped = 1e-12
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


# ── Natural exit scanner ───────────────────────────────────────────────

def _find_natural_exit(
    i_exit_d: int,
    N: int,
    td,
    luld_states: list,
    luld_fire_sides: list,
    epg_states: list,
) -> tuple[int, int, float, str]:
    """Scan forward from i_exit_d to find next LULD or EPG close exit.

    Called only when EXIT_D fires, to record what the exit would have been
    without EXIT_D (needed for T10 sensitivity sweep re-simulation).
    Returns (exit_idx, exit_ts_ns, exit_price, exit_reason).
    """
    prev_epg = epg_states[i_exit_d]  # PASS at EXIT_D fire tick
    for j in range(i_exit_d + 1, N):
        if luld_states[j] == ProximityState.EXIT_HALT:
            fill_j = min(j + 1, N - 1)
            side = luld_fire_sides[j] or "lower"
            return j, int(td.timestamps[j]), float(td.prices[fill_j]), f"luld_{side}"
        cur_epg = epg_states[j]
        if prev_epg == GateState.PASS and cur_epg != GateState.PASS:
            fill_j = min(j + 1, N - 1)
            return j, int(td.timestamps[j]), float(td.prices[fill_j]), "epg_window_close"
        prev_epg = cur_epg
    return N - 1, int(td.timestamps[N - 1]), float(td.prices[N - 1]), "session_end"


# ══════════════════════════════════════════════════════════════════════
#  WORKER — Process one event
# ══════════════════════════════════════════════════════════════════════


def _process_event(args: dict) -> dict:
    """Run the Phase U backtest on one event.

    Returns a dict of:
        status: 'event'   -> includes trades list and per-event aggregates
        status: 'skipped' -> includes reason
        status: 'error'   -> includes error + traceback
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    event_idx = args["event_idx"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    gap_threshold = args["gap_threshold"]
    exit_d_enabled = args["exit_d_enabled"]
    exit_d_theta = args["exit_d_theta"]
    exit_d_tau_min_ns = args["exit_d_tau_min_ns"]   # pre-converted to int ns
    luld_ref_window_sec = args["luld_ref_window_sec"]
    luld_proximity_threshold = args.get("luld_proximity_threshold", 0.02)
    luld_exit_duration_sec = args.get("luld_exit_duration_sec", 0.0)
    luld_lower_band_enabled = args.get("luld_lower_band_enabled", True)
    luld_warmup_sec = args["luld_warmup_sec"]
    reentry_enabled = args["reentry_enabled"]
    reentry_tau_recovery_sec = args["reentry_tau_recovery_sec"]
    gap_gate_enabled = args.get("gap_gate_enabled", True)
    watermark_threshold = args.get("watermark_threshold", None)
    cvd_filter_enabled = args.get("cvd_filter_enabled", False)
    intra_window_watermark_threshold = args.get("intra_window_watermark_threshold", None)
    epg_cfg = args.get("epg_cfg", {})
    gate_mode = epg_cfg.get("gate_mode", "peak")
    epg_tau_peak = epg_cfg.get("tau_peak", 600.0)
    epg_C = epg_cfg.get("C", 2.0)

    base = {"ticker": ticker, "date": date, "event_idx": event_idx}

    try:
        # ── 1. Load data ──
        td = load_trades(ticker, date, mom_pct)
        if td is None or td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        # ── 1b. Prev close ──
        prev_close = get_prev_close(ticker, date)
        if prev_close is None or prev_close <= 0:
            return {**base, "status": "skipped", "reason": "missing_prev_close"}

        # ── 1c. Setup filter ──
        start_ns, end_ns = _session_ns_bounds(date)
        sf = run_setup_filter(
            timestamps=td.timestamps,
            prices=td.prices,
            sizes=td.sizes,
            session_start_ns=start_ns,
            session_end_ns=end_ns,
        )
        N = td.n_trades

        # ── 2. Lee-Ready sides ──
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

        # ── 3. Hawkes replay with online refit ──
        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)
        dv_arr = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
        if gate_mode == "background":
            mu_buy_out_arr = np.zeros(N, dtype=np.float64)
            mu_sell_out_arr = np.zeros(N, dtype=np.float64)
            dbar_out_arr = np.zeros(N, dtype=np.float64)
        else:
            mu_buy_out_arr = mu_sell_out_arr = dbar_out_arr = None

        global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        if math.isnan(per_event_lref) or per_event_lref <= 0:
            lambda_ref = global_lambda_ref
        else:
            lambda_ref = per_event_lref

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
            dv_arr=dv_arr,
            mu_buy_out=mu_buy_out_arr, mu_sell_out=mu_sell_out_arr,
            dbar_out=dbar_out_arr,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # ── 4. EPG ──
        global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)
        gate = ParticipationGate(
            half_life_seconds=EPG_TAU,
            peak_threshold_p=EPG_P,
            warmup_seconds=EPG_WARMUP,
            gate_mode=gate_mode,
            tau_peak=epg_tau_peak,
            C=epg_C,
        )

        epg_states = [GateState.INACTIVE] * N
        t_event_fired = False
        t_event_idx = None
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None and not t_event_fired:
                gate.activate(t_ev)
                t_event_fired = True
                t_event_idx = i
            dv = float(td.prices[i]) * float(td.sizes[i])
            if gate_mode == "background":
                epg_states[i] = gate.update(
                    dv, td.t_sec[i],
                    mu_buy=mu_buy_out_arr[i],
                    mu_sell=mu_sell_out_arr[i],
                    lambda_buy=lam_buy_out[i],
                    lambda_sell=lam_sell_out[i],
                    dbar=dbar_out_arr[i],
                )
            else:
                epg_states[i] = gate.update(dv, td.t_sec[i])

        if not t_event_fired:
            return {**base, "status": "skipped", "reason": "no_t_event",
                    "n_trades_in_event": N}

        # ── 5. Pre-compute LULD states (one pass; avoids stateful re-entry) ──
        # EXIT priority: EXIT_D > LULD > EPG window close.
        # Calling update() in a pre-pass lets _find_natural_exit() query
        # luld_states[] without re-entering the stateful buffer.
        # Quote pointer uses the same pattern as the Lee-Ready classifier in
        # core/ofi/trade_ofi.py: advance q_idx until the next quote is after
        # the current trade timestamp, then use the prevailing quote.
        luld_pre = LuldProximityExit(
            ref_window_sec=luld_ref_window_sec,
            proximity_threshold=luld_proximity_threshold,
            warmup_sec=luld_warmup_sec,
            luld_exit_duration_sec=luld_exit_duration_sec,
        )
        luld_states = []
        luld_fire_sides = []
        luld_fallback_count = 0
        nq = qd.n_quotes
        q_idx_luld = 0
        for i in range(N):
            while q_idx_luld < nq - 1 and qd.timestamps[q_idx_luld + 1] <= td.timestamps[i]:
                q_idx_luld += 1
            if q_idx_luld < nq and qd.timestamps[q_idx_luld] <= td.timestamps[i]:
                bid_q = float(qd.bid_prices[q_idx_luld])
                ask_q = float(qd.ask_prices[q_idx_luld])
            else:
                bid_q = None
                ask_q = None
            if (bid_q is None or ask_q is None
                    or ask_q <= bid_q or bid_q <= 0.0):
                luld_fallback_count += 1
            lr = luld_pre.update(int(td.timestamps[i]), float(td.prices[i]), bid_q, ask_q)
            luld_states.append(lr.state)
            luld_fire_sides.append(lr.fire_side)
        if N > 0 and luld_fallback_count / N > 0.10:
            log.warning(
                f"LULD fallback rate {luld_fallback_count}/{N}="
                f"{100*luld_fallback_count/N:.1f}% > 10%% for {ticker} {date}"
            )

        # ── 5b. Re-entry signal (one per event; reset between uses) ──
        reentry_sig = ReentrySignal(
            theta=exit_d_theta,
            tau_recovery_sec=reentry_tau_recovery_sec,
        )

        # ── 6. PASS window durations (observational, for stats) ──
        session_start_ns, _ = _session_ns_bounds(date)
        pass_window_durations = []
        run_start = None
        for k in range(N):
            cur = epg_states[k]
            if cur == GateState.PASS and run_start is None:
                run_start = td.t_sec[k]
            elif cur != GateState.PASS and run_start is not None:
                pass_window_durations.append(td.t_sec[k] - run_start)
                run_start = None
        if run_start is not None:
            pass_window_durations.append(td.t_sec[N - 1] - run_start)

        # ── 7. Sequential entry/exit state machine ──
        trades = []
        gap_gate_blocks = 0
        pass_edges_total = 0
        gap_gate_queued_entries = 0

        in_position = False
        gap_gate_queued = False
        entry_idx = None
        entry_price = None
        entry_t_sec = None
        intraday_pct_at_entry = None
        entry_drawdown_from_high = 0.0
        entry_cvd_at_entry = 0.0
        position_max_price: Optional[float] = None

        # Per-position EXIT_D state
        exit_d_disabled = False
        dump_timer_start_ns = None

        # Re-entry state
        waiting_for_reentry = False
        n_reentries = 0
        current_entry_type = "first"

        # Backside filter state (Phase C)
        high_watermark_price = None
        cvd_since_t_event = 0.0
        n_watermark_blocks = 0
        n_cvd_blocks = 0

        # Phase D intra-window rolling high
        current_window_high = None
        prior_window_peak = None
        first_window_seen = False
        n_intra_window_blocks = 0
        n_re_entries_intra_blocked = 0
        blocked_edges = []
        entry_dwh = None   # drawdown_from_window_high at entry
        entry_cwh = None   # current_window_high at entry
        entry_pwp = None   # prior_window_peak at entry

        prev_state = GateState.INACTIVE

        for i in range(N):
            cur = epg_states[i]
            cur_luld = luld_states[i]

            # ── Backside filter tracking: update from T_event onwards ──
            if t_event_idx is not None and i >= t_event_idx:
                cur_p_tick = float(td.prices[i])
                if high_watermark_price is None:
                    high_watermark_price = cur_p_tick
                else:
                    high_watermark_price = max(high_watermark_price, cur_p_tick)
                if cvd_filter_enabled:
                    cvd_since_t_event += cur_p_tick * float(td.sizes[i]) * float(sides[i])

            # ── Phase D: intra-window rolling high tracking ──
            if intra_window_watermark_threshold is not None:
                if cur == GateState.PASS and prev_state == GateState.PASS:
                    if current_window_high is not None:
                        current_window_high = max(current_window_high, float(td.prices[i]))
                elif prev_state == GateState.PASS and cur != GateState.PASS:
                    prior_window_peak = current_window_high
                    current_window_high = None

            if not in_position:
                # ── Gap gate queued: clear if PASS window ended ──
                if gap_gate_queued and cur != GateState.PASS:
                    gap_gate_queued = False
                    gap_gate_blocks += 1  # window closed before gap was met

                # ── Re-entry monitoring (after EXIT_D fired, within PASS window) ──
                if waiting_for_reentry:
                    if cur != GateState.PASS:
                        # PASS window closed — cancel re-entry wait
                        waiting_for_reentry = False
                        reentry_sig.reset()
                    elif reentry_enabled:
                        if reentry_sig.update(
                            int(td.timestamps[i]),
                            lam_buy_out[i], lam_sell_out[i], cur,
                        ):
                            re_cur_p = float(td.prices[i])

                            # Phase D: intra-window drawdown check for re-entry
                            re_blocked = False
                            if intra_window_watermark_threshold is not None:
                                if current_window_high is not None and current_window_high > 0:
                                    if current_window_high < re_cur_p - 1e-8:
                                        raise RuntimeError(
                                            f"ESCALATION: current_window_high="
                                            f"{current_window_high} < re_entry_price="
                                            f"{re_cur_p} at {ticker} {date} tick {i}"
                                        )
                                    re_dfw = max(
                                        0.0,
                                        (current_window_high - re_cur_p)
                                        / current_window_high,
                                    )
                                    if re_dfw > intra_window_watermark_threshold:
                                        n_intra_window_blocks += 1
                                        n_re_entries_intra_blocked += 1
                                        re_blocked = True
                                        blocked_edges.append({
                                            "ticker": ticker, "date": date,
                                            "event_idx": event_idx,
                                            "entry_ts": int(td.timestamps[i]),
                                            "entry_type": "reentry",
                                            "drawdown_from_window_high": float(re_dfw),
                                            "current_window_high": float(current_window_high),
                                            "prior_window_peak": (
                                                float(prior_window_peak)
                                                if prior_window_peak is not None else None
                                            ),
                                            "entry_blocked": True,
                                        })
                                        reentry_sig.reset()
                                        waiting_for_reentry = False

                            if not re_blocked:
                                if not sf.passes:
                                    reentry_sig.reset()
                                    waiting_for_reentry = False
                                    re_blocked = True

                            if not re_blocked:
                                entry_price = float(td.prices[min(i + 1, N - 1)])
                                entry_idx = i
                                entry_t_sec = td.t_sec[i]
                                intraday_pct_at_entry = (
                                    (float(td.prices[i]) - prev_close) / prev_close
                                )
                                in_position = True
                                waiting_for_reentry = False
                                reentry_sig.reset()
                                n_reentries += 1
                                current_entry_type = "reentry"
                                entry_drawdown_from_high = (
                                    (high_watermark_price - re_cur_p) / high_watermark_price
                                    if high_watermark_price and high_watermark_price > 0 else 0.0
                                )
                                entry_cvd_at_entry = cvd_since_t_event
                                if intra_window_watermark_threshold is not None:
                                    _re_dfw = (
                                        max(0.0, (current_window_high - re_cur_p)
                                            / current_window_high)
                                        if current_window_high else 0.0
                                    )
                                    entry_dwh = _re_dfw
                                    entry_cwh = current_window_high
                                    entry_pwp = prior_window_peak
                                lam_tot_e = lam_buy_out[i] + lam_sell_out[i]
                                I_entry = (lam_sell_out[i] / lam_tot_e
                                           if lam_tot_e > 0 else 0.0)
                                exit_d_disabled = (not math.isnan(I_entry)
                                                   and I_entry > exit_d_theta)
                                dump_timer_start_ns = None

                # ── Normal rising-edge entry (only when not in re-entry wait) ──
                if not in_position and not waiting_for_reentry:
                    rising_edge = (
                        cur == GateState.PASS
                        and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
                    )

                    if rising_edge:
                        pass_edges_total += 1
                        cur_price = float(td.prices[i])

                        # ── Backside filter checks ──
                        entry_blocked = False
                        drawdown_from_high_at_edge = 0.0
                        _dfw_edge = 0.0

                        if intra_window_watermark_threshold is not None:
                            # Phase D: initialize intra-window rolling high on rising edge
                            if not first_window_seen:
                                if prior_window_peak is not None:
                                    raise RuntimeError(
                                        f"T1d ESCALATION: prior_window_peak="
                                        f"{prior_window_peak} non-None on first "
                                        f"PASS window of {ticker} {date}"
                                    )
                                first_window_seen = True
                            current_window_high = (
                                max(cur_price, prior_window_peak)
                                if prior_window_peak is not None else cur_price
                            )
                            if current_window_high < cur_price - 1e-8:
                                raise RuntimeError(
                                    f"ESCALATION: current_window_high="
                                    f"{current_window_high} < current_price="
                                    f"{cur_price} at {ticker} {date} tick {i}"
                                )
                            _dfw_edge = (
                                max(0.0, (current_window_high - cur_price)
                                    / current_window_high)
                                if current_window_high > 0 else 0.0
                            )
                            if _dfw_edge > intra_window_watermark_threshold:
                                n_intra_window_blocks += 1
                                entry_blocked = True
                                blocked_edges.append({
                                    "ticker": ticker, "date": date,
                                    "event_idx": event_idx,
                                    "entry_ts": int(td.timestamps[i]),
                                    "entry_type": "first",
                                    "drawdown_from_window_high": float(_dfw_edge),
                                    "current_window_high": float(current_window_high),
                                    "prior_window_peak": (
                                        float(prior_window_peak)
                                        if prior_window_peak is not None else None
                                    ),
                                    "entry_blocked": True,
                                })
                        elif (watermark_threshold is not None
                              and high_watermark_price is not None
                              and high_watermark_price > 0):
                            drawdown_from_high_at_edge = (
                                (high_watermark_price - cur_price) / high_watermark_price
                            )
                            if drawdown_from_high_at_edge > watermark_threshold:
                                n_watermark_blocks += 1
                                entry_blocked = True
                        if not entry_blocked and cvd_filter_enabled:
                            if cvd_since_t_event < 0:
                                n_cvd_blocks += 1
                                entry_blocked = True

                        if not entry_blocked:
                            if gap_gate_enabled:
                                intraday_pct = (cur_price - prev_close) / prev_close
                                if intraday_pct >= gap_threshold:
                                    # Immediate entry
                                    if i + 1 < N:
                                        entry_price = float(td.prices[i + 1])
                                    else:
                                        entry_price = cur_price
                                    entry_idx = i
                                    entry_t_sec = td.t_sec[i]
                                    intraday_pct_at_entry = intraday_pct
                                    in_position = True
                                    gap_gate_queued = False
                                    current_entry_type = "first"
                                    entry_drawdown_from_high = drawdown_from_high_at_edge
                                    entry_cvd_at_entry = cvd_since_t_event
                                    if intra_window_watermark_threshold is not None:
                                        entry_dwh = _dfw_edge
                                        entry_cwh = current_window_high
                                        entry_pwp = prior_window_peak
                                    lam_tot_e = lam_buy_out[i] + lam_sell_out[i]
                                    I_entry = (lam_sell_out[i] / lam_tot_e
                                               if lam_tot_e > 0 else 0.0)
                                    exit_d_disabled = (not math.isnan(I_entry)
                                                       and I_entry > exit_d_theta)
                                    dump_timer_start_ns = None
                                else:
                                    # Gap not yet met — enter queued wait
                                    gap_gate_queued = True
                            else:
                                # Gap gate disabled — immediate entry on every rising edge
                                intraday_pct = (cur_price - prev_close) / prev_close
                                if i + 1 < N:
                                    entry_price = float(td.prices[i + 1])
                                else:
                                    entry_price = cur_price
                                entry_idx = i
                                entry_t_sec = td.t_sec[i]
                                intraday_pct_at_entry = intraday_pct
                                in_position = True
                                current_entry_type = "first"
                                entry_drawdown_from_high = drawdown_from_high_at_edge
                                entry_cvd_at_entry = cvd_since_t_event
                                if intra_window_watermark_threshold is not None:
                                    entry_dwh = _dfw_edge
                                    entry_cwh = current_window_high
                                    entry_pwp = prior_window_peak
                                lam_tot_e = lam_buy_out[i] + lam_sell_out[i]
                                I_entry = (lam_sell_out[i] / lam_tot_e
                                           if lam_tot_e > 0 else 0.0)
                                exit_d_disabled = (not math.isnan(I_entry)
                                                   and I_entry > exit_d_theta)
                                dump_timer_start_ns = None

                    elif gap_gate_queued and cur == GateState.PASS:
                        # Still in same PASS window; re-check gap on each tick
                        cur_price = float(td.prices[i])
                        intraday_pct = (cur_price - prev_close) / prev_close
                        if intraday_pct >= gap_threshold:
                            if i + 1 < N:
                                entry_price = float(td.prices[i + 1])
                            else:
                                entry_price = cur_price
                            entry_idx = i
                            entry_t_sec = td.t_sec[i]
                            intraday_pct_at_entry = intraday_pct
                            in_position = True
                            gap_gate_queued = False
                            gap_gate_queued_entries += 1
                            current_entry_type = "first"
                            entry_drawdown_from_high = 0.0
                            entry_cvd_at_entry = cvd_since_t_event
                            if intra_window_watermark_threshold is not None:
                                _q_p = float(td.prices[i])
                                entry_dwh = (
                                    max(0.0, (current_window_high - _q_p)
                                        / current_window_high)
                                    if current_window_high else 0.0
                                )
                                entry_cwh = current_window_high
                                entry_pwp = prior_window_peak
                            lam_tot_e = lam_buy_out[i] + lam_sell_out[i]
                            I_entry = (lam_sell_out[i] / lam_tot_e
                                       if lam_tot_e > 0 else 0.0)
                            exit_d_disabled = (not math.isnan(I_entry)
                                               and I_entry > exit_d_theta)
                            dump_timer_start_ns = None

            else:
                # ── In position: check exits in priority order ──
                position_max_price = max(
                    position_max_price if position_max_price is not None else entry_price,
                    float(td.prices[i]),
                )
                exit_fired = False

                # Priority 1: EXIT_D
                if exit_d_enabled and not exit_d_disabled:
                    lam_tot = lam_buy_out[i] + lam_sell_out[i]
                    I_t = lam_sell_out[i] / lam_tot if lam_tot > 0 else 0.0
                    if not math.isnan(I_t) and I_t > exit_d_theta:
                        if dump_timer_start_ns is None:
                            dump_timer_start_ns = int(td.timestamps[i])
                        elif (int(td.timestamps[i]) - dump_timer_start_ns
                              >= exit_d_tau_min_ns):
                            # EXIT_D fires — fill at next tick
                            fill_idx = min(i + 1, N - 1)
                            exit_price = float(td.prices[fill_idx])
                            exit_t_sec = td.t_sec[i]
                            pnl_pct = ((exit_price - entry_price)
                                       / entry_price * 100.0)
                            tod_sec = float(
                                td.timestamps[entry_idx] - session_start_ns
                            ) / NS_PER_SECOND
                            # Natural exit: what would have fired without EXIT_D
                            nat_idx, nat_ts, nat_price, nat_reason = \
                                _find_natural_exit(i, N, td, luld_states,
                                                   luld_fire_sides, epg_states)
                            nat_pnl = ((nat_price - entry_price)
                                       / entry_price * 100.0)
                            trades.append({
                                "ticker": ticker, "date": date,
                                "event_idx": event_idx,
                                "trade_seq": len(trades),
                                "entry_idx": entry_idx, "exit_idx": i,
                                "entry_ts": int(td.timestamps[entry_idx]),
                                "exit_ts": int(td.timestamps[i]),
                                "entry_t_sec": float(entry_t_sec),
                                "exit_t_sec": float(exit_t_sec),
                                "hold_sec": float(exit_t_sec - entry_t_sec),
                                "entry_price": float(entry_price),
                                "exit_price": float(exit_price),
                                "pnl_pct": float(pnl_pct),
                                "intraday_pct_at_entry": float(intraday_pct_at_entry),
                                "prev_close": float(prev_close),
                                "time_of_day_sec": float(tod_sec),
                                "session_bucket": session_bucket(tod_sec),
                                "entry_type": current_entry_type,
                                "exit_reason": "exit_d",
                                "drawdown_from_high": float(entry_drawdown_from_high),
                                "cvd_at_entry": float(entry_cvd_at_entry),
                                "natural_exit_idx": int(nat_idx),
                                "natural_exit_ts": int(nat_ts),
                                "natural_exit_price": float(nat_price),
                                "natural_exit_pnl_pct": float(nat_pnl),
                                "natural_exit_reason": nat_reason,
                                "drawdown_from_window_high": entry_dwh,
                                "current_window_high_at_entry": entry_cwh,
                                "prior_window_peak_at_entry": entry_pwp,
                                "max_price_during_hold": float(position_max_price) if position_max_price is not None else float(entry_price),
                            })
                            in_position = False
                            entry_idx = entry_price = entry_t_sec = None
                            intraday_pct_at_entry = None
                            exit_d_disabled = False
                            dump_timer_start_ns = None
                            entry_dwh = entry_cwh = entry_pwp = None
                            position_max_price = None
                            if reentry_enabled:
                                waiting_for_reentry = True
                                reentry_sig.reset()
                            exit_fired = True
                    else:
                        dump_timer_start_ns = None  # timer reset

                # Priority 2: LULD proximity
                if not exit_fired and cur_luld == ProximityState.EXIT_HALT:
                    _luld_side = luld_fire_sides[i] or "lower"
                    # Gate: lower-band fire suppressed when lower_band_enabled=False
                    if _luld_side == "lower" and not luld_lower_band_enabled:
                        log.debug(
                            f"LULD lower-band suppressed at tick {i}: {ticker} {date}"
                        )
                    else:
                        fill_idx = min(i + 1, N - 1)
                        exit_price = float(td.prices[fill_idx])
                        exit_t_sec = td.t_sec[i]
                        pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                        tod_sec = float(
                            td.timestamps[entry_idx] - session_start_ns
                        ) / NS_PER_SECOND
                        _luld_reason = f"luld_{_luld_side}"
                        trades.append({
                            "ticker": ticker, "date": date,
                            "event_idx": event_idx,
                            "trade_seq": len(trades),
                            "entry_idx": entry_idx, "exit_idx": i,
                            "entry_ts": int(td.timestamps[entry_idx]),
                            "exit_ts": int(td.timestamps[i]),
                            "entry_t_sec": float(entry_t_sec),
                            "exit_t_sec": float(exit_t_sec),
                            "hold_sec": float(exit_t_sec - entry_t_sec),
                            "entry_price": float(entry_price),
                            "exit_price": float(exit_price),
                            "pnl_pct": float(pnl_pct),
                            "intraday_pct_at_entry": float(intraday_pct_at_entry),
                            "prev_close": float(prev_close),
                            "time_of_day_sec": float(tod_sec),
                            "session_bucket": session_bucket(tod_sec),
                            "entry_type": current_entry_type,
                            "exit_reason": _luld_reason,
                            "drawdown_from_high": float(entry_drawdown_from_high),
                            "cvd_at_entry": float(entry_cvd_at_entry),
                            "natural_exit_idx": i,
                            "natural_exit_ts": int(td.timestamps[i]),
                            "natural_exit_price": float(exit_price),
                            "natural_exit_pnl_pct": float(pnl_pct),
                            "natural_exit_reason": _luld_reason,
                            "drawdown_from_window_high": entry_dwh,
                            "current_window_high_at_entry": entry_cwh,
                            "prior_window_peak_at_entry": entry_pwp,
                            "max_price_during_hold": float(position_max_price) if position_max_price is not None else float(entry_price),
                        })
                        in_position = False
                        entry_idx = entry_price = entry_t_sec = None
                        intraday_pct_at_entry = None
                        exit_d_disabled = False
                        dump_timer_start_ns = None
                        entry_dwh = entry_cwh = entry_pwp = None
                        position_max_price = None
                        exit_fired = True

                # Priority 3: EPG window close
                if (not exit_fired
                        and prev_state == GateState.PASS
                        and cur != GateState.PASS):
                    if i + 1 < N:
                        exit_price = float(td.prices[i + 1])
                    else:
                        exit_price = float(td.prices[i])
                    exit_t_sec = td.t_sec[i]
                    pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                    tod_sec = float(
                        td.timestamps[entry_idx] - session_start_ns
                    ) / NS_PER_SECOND
                    trades.append({
                        "ticker": ticker, "date": date,
                        "event_idx": event_idx,
                        "trade_seq": len(trades),
                        "entry_idx": entry_idx, "exit_idx": i,
                        "entry_ts": int(td.timestamps[entry_idx]),
                        "exit_ts": int(td.timestamps[i]),
                        "entry_t_sec": float(entry_t_sec),
                        "exit_t_sec": float(exit_t_sec),
                        "hold_sec": float(exit_t_sec - entry_t_sec),
                        "entry_price": float(entry_price),
                        "exit_price": float(exit_price),
                        "pnl_pct": float(pnl_pct),
                        "intraday_pct_at_entry": float(intraday_pct_at_entry),
                        "prev_close": float(prev_close),
                        "time_of_day_sec": float(tod_sec),
                        "session_bucket": session_bucket(tod_sec),
                        "entry_type": current_entry_type,
                        "exit_reason": "epg_window_close",
                        "drawdown_from_high": float(entry_drawdown_from_high),
                        "cvd_at_entry": float(entry_cvd_at_entry),
                        "natural_exit_idx": i,
                        "natural_exit_ts": int(td.timestamps[i]),
                        "natural_exit_price": float(exit_price),
                        "natural_exit_pnl_pct": float(pnl_pct),
                        "natural_exit_reason": "epg_window_close",
                        "drawdown_from_window_high": entry_dwh,
                        "current_window_high_at_entry": entry_cwh,
                        "prior_window_peak_at_entry": entry_pwp,
                        "max_price_during_hold": float(position_max_price) if position_max_price is not None else float(entry_price),
                    })
                    in_position = False
                    entry_idx = entry_price = entry_t_sec = None
                    intraday_pct_at_entry = None
                    exit_d_disabled = False
                    dump_timer_start_ns = None
                    entry_dwh = entry_cwh = entry_pwp = None
                    position_max_price = None

            prev_state = cur

        # ── If still in position at session end ──
        if in_position:
            exit_price = float(td.prices[N - 1])
            exit_t_sec = td.t_sec[N - 1]
            pnl_pct = (exit_price - entry_price) / entry_price * 100.0
            tod_sec = float(
                td.timestamps[entry_idx] - session_start_ns
            ) / NS_PER_SECOND
            trades.append({
                "ticker": ticker, "date": date,
                "event_idx": event_idx,
                "trade_seq": len(trades),
                "entry_idx": entry_idx, "exit_idx": N - 1,
                "entry_ts": int(td.timestamps[entry_idx]),
                "exit_ts": int(td.timestamps[N - 1]),
                "entry_t_sec": float(entry_t_sec),
                "exit_t_sec": float(exit_t_sec),
                "hold_sec": float(exit_t_sec - entry_t_sec),
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "pnl_pct": float(pnl_pct),
                "intraday_pct_at_entry": float(intraday_pct_at_entry),
                "prev_close": float(prev_close),
                "time_of_day_sec": float(tod_sec),
                "session_bucket": session_bucket(tod_sec),
                "entry_type": current_entry_type,
                "exit_reason": "session_end",
                "drawdown_from_high": float(entry_drawdown_from_high),
                "cvd_at_entry": float(entry_cvd_at_entry),
                "natural_exit_idx": N - 1,
                "natural_exit_ts": int(td.timestamps[N - 1]),
                "natural_exit_price": float(exit_price),
                "natural_exit_pnl_pct": float(pnl_pct),
                "natural_exit_reason": "session_end",
                "drawdown_from_window_high": entry_dwh,
                "current_window_high_at_entry": entry_cwh,
                "prior_window_peak_at_entry": entry_pwp,
                "max_price_during_hold": float(position_max_price) if position_max_price is not None else float(entry_price),
            })

        max_intraday_pct = float(
            (float(np.max(td.prices)) - prev_close) / prev_close
        )

        return {
            **base,
            "status": "event",
            "trades": trades,
            "n_trades_in_event": int(len(trades)),
            "n_reentries_in_event": int(n_reentries),
            "n_pass_edges": int(pass_edges_total),
            "n_gap_gate_blocks": int(gap_gate_blocks),
            "n_gap_gate_queued_entries": int(gap_gate_queued_entries),
            "gap_gate_enabled": gap_gate_enabled,
            "n_watermark_blocks": int(n_watermark_blocks),
            "n_cvd_blocks": int(n_cvd_blocks),
            "n_intra_window_blocks": int(n_intra_window_blocks),
            "n_re_entries_intra_blocked": int(n_re_entries_intra_blocked),
            "blocked_edges": blocked_edges,
            "n_luld_fallback": int(luld_fallback_count),
            "n_pass_windows": int(len(pass_window_durations)),
            "mean_pass_window_sec": (float(np.mean(pass_window_durations))
                                     if pass_window_durations else 0.0),
            "median_pass_window_sec": (float(np.median(pass_window_durations))
                                       if pass_window_durations else 0.0),
            "max_intraday_pct_session": max_intraday_pct,
            "prev_close": float(prev_close),
            "first_price": float(td.prices[0]),
            "last_price": float(td.prices[-1]),
            "n_event_trades": int(N),
            "pass_window_durations": pass_window_durations,
        }

    except Exception as e:
        return {
            **base,
            "status": "error",
            "error": str(e),
            "traceback": tb_module.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  METRICS
# ══════════════════════════════════════════════════════════════════════


def compute_run_summary(events: list[dict]) -> dict:
    all_trades = []
    for ev in events:
        if ev.get("status") == "event":
            all_trades.extend(ev["trades"])

    n_trades = len(all_trades)
    if n_trades == 0:
        return {"n_trades": 0, "profit_factor": None, "warning": "no trades"}

    pnl = np.array([t["pnl_pct"] for t in all_trades])
    hold = np.array([t["hold_sec"] for t in all_trades])
    intraday = np.array([t["intraday_pct_at_entry"] for t in all_trades])

    wins = pnl > 0
    losses = pnl < 0
    win_sum = float(np.sum(pnl[wins])) if wins.any() else 0.0
    loss_sum = float(np.abs(np.sum(pnl[losses]))) if losses.any() else 1e-10
    pf = win_sum / loss_sum

    # Per-session breakdown
    by_session = defaultdict(list)
    for t in all_trades:
        by_session[t["session_bucket"]].append(t)
    session_breakdown = {}
    for bucket, trades_b in by_session.items():
        p = np.array([t["pnl_pct"] for t in trades_b])
        w = p > 0; l = p < 0
        w_sum = float(np.sum(p[w])) if w.any() else 0.0
        l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
        session_breakdown[bucket] = {
            "n_trades": len(trades_b),
            "profit_factor": round(w_sum / l_sum, 4),
            "mean_pnl_pct": round(float(np.mean(p)), 4),
            "win_rate": round(float(np.mean(w)) * 100, 2),
        }

    # Entry type breakdown
    by_entry_type = defaultdict(list)
    for t in all_trades:
        by_entry_type[t.get("entry_type", "first")].append(t)
    entry_type_breakdown = {}
    for etype, trades_e in by_entry_type.items():
        p = np.array([t["pnl_pct"] for t in trades_e])
        w = p > 0; l = p < 0
        w_sum = float(np.sum(p[w])) if w.any() else 0.0
        l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
        entry_type_breakdown[etype] = {
            "count": len(trades_e),
            "pct_of_trades": round(100 * len(trades_e) / n_trades, 2),
            "profit_factor": round(w_sum / l_sum, 4),
            "win_rate": round(float(np.mean(w)) * 100, 2),
            "mean_pnl_pct": round(float(np.mean(p)), 4),
            "mean_hold_sec": round(float(np.mean([t["hold_sec"] for t in trades_e])), 2),
        }

    # Exit reason breakdown
    by_exit = defaultdict(list)
    for t in all_trades:
        by_exit[t["exit_reason"]].append(t)
    exit_breakdown = {}
    for reason, trades_e in by_exit.items():
        p = np.array([t["pnl_pct"] for t in trades_e])
        w = p > 0; l = p < 0
        w_sum = float(np.sum(p[w])) if w.any() else 0.0
        l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
        exit_breakdown[reason] = {
            "count": len(trades_e),
            "pct_of_trades": round(100 * len(trades_e) / n_trades, 2),
            "profit_factor": round(w_sum / l_sum, 4),
            "mean_pnl_pct": round(float(np.mean(p)), 4),
        }

    # Gap gate stats
    n_pass_edges = sum(
        ev.get("n_pass_edges", 0) for ev in events if ev.get("status") == "event"
    )
    n_gap_blocks = sum(
        ev.get("n_gap_gate_blocks", 0) for ev in events if ev.get("status") == "event"
    )
    n_queued_entries = sum(
        ev.get("n_gap_gate_queued_entries", 0)
        for ev in events if ev.get("status") == "event"
    )
    gap_block_pct = (100 * n_gap_blocks / n_pass_edges
                     if n_pass_edges > 0 else 0.0)
    pct_queued = (100 * n_queued_entries / n_trades
                  if n_trades > 0 else 0.0)

    # Backside filter stats (Phase C / Phase D)
    n_watermark_blocks = sum(
        ev.get("n_watermark_blocks", 0) for ev in events if ev.get("status") == "event"
    )
    n_cvd_blocks = sum(
        ev.get("n_cvd_blocks", 0) for ev in events if ev.get("status") == "event"
    )
    n_intra_window_blocks = sum(
        ev.get("n_intra_window_blocks", 0) for ev in events if ev.get("status") == "event"
    )
    n_re_entries_intra_blocked = sum(
        ev.get("n_re_entries_intra_blocked", 0) for ev in events if ev.get("status") == "event"
    )
    n_luld_fallback = sum(
        ev.get("n_luld_fallback", 0) for ev in events if ev.get("status") == "event"
    )
    n_entries_blocked = n_watermark_blocks + n_cvd_blocks + n_intra_window_blocks

    intraday_q = np.percentile(intraday, [25, 50, 75]) * 100
    intraday_summary = {
        "mean_pct": round(float(np.mean(intraday) * 100), 2),
        "median_pct": round(float(intraday_q[1]), 2),
        "p25_pct": round(float(intraday_q[0]), 2),
        "p75_pct": round(float(intraday_q[2]), 2),
        "min_pct": round(float(np.min(intraday) * 100), 2),
        "max_pct": round(float(np.max(intraday) * 100), 2),
    }

    return {
        "n_trades": int(n_trades),
        "profit_factor": round(pf, 4),
        "win_rate": round(float(np.mean(wins)) * 100, 2),
        "mean_pnl_pct": round(float(np.mean(pnl)), 4),
        "median_pnl_pct": round(float(np.median(pnl)), 4),
        "total_pnl_pct": round(float(np.sum(pnl)), 4),
        "max_win_pct": round(float(np.max(pnl)), 4),
        "max_loss_pct": round(float(np.min(pnl)), 4),
        "mean_hold_sec": round(float(np.mean(hold)), 2),
        "median_hold_sec": round(float(np.median(hold)), 2),
        "session_breakdown": session_breakdown,
        "entry_type_breakdown": entry_type_breakdown,
        "exit_reason_breakdown": exit_breakdown,
        "gap_gate": {
            "pass_edges_total": int(n_pass_edges),
            "blocked_by_gap": int(n_gap_blocks),
            "block_pct": round(gap_block_pct, 2),
            "queued_entries": int(n_queued_entries),
            "pct_entries_queued": round(pct_queued, 2),
            "intraday_pct_at_entry": intraday_summary,
        },
        "backside_filter": {
            "n_watermark_blocks": int(n_watermark_blocks),
            "n_cvd_blocks": int(n_cvd_blocks),
            "n_intra_window_blocks": int(n_intra_window_blocks),
            "n_re_entries_intra_blocked": int(n_re_entries_intra_blocked),
            "n_entries_blocked": int(n_entries_blocked),
        },
        "luld_fallback_ticks": int(n_luld_fallback),
    }


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════


def parse_args():
    parser = argparse.ArgumentParser(description="Phase U — Screening + EXIT_D + LULD Runner")
    parser.add_argument("--split", type=str, default="val",
                        choices=["train", "val", "trainval", "test"],
                        help="Split to run on (test forbidden)")
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--random-sample", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--gap-threshold", type=float, default=None,
                        help="Override gap threshold from config")
    parser.add_argument("--exit-d-theta", type=float, default=None,
                        help="Override EXIT_D theta from config (for sensitivity sweep)")
    parser.add_argument("--exit-d-tau-min", type=float, default=None,
                        help="Override EXIT_D tau_min_sec from config (for sensitivity sweep)")
    parser.add_argument("--config", type=str, default=None,
                        help="Path to strategy.json config (default: config/strategy.json)")
    parser.add_argument("--results-dir", type=str, default=None)
    parser.add_argument("--ticker", type=str, default=None,
                        help="Filter to single ticker (for smoke test)")
    parser.add_argument("--date", type=str, default=None,
                        help="Filter to single date (for smoke test)")
    parser.add_argument("--no-gap-gate", action="store_true", default=False,
                        help="Disable gap gate — enter on every PASS rising edge (Phase C)")
    parser.add_argument("--watermark-threshold", type=float, default=None,
                        help="Watermark drawdown filter threshold (Phase C Option A)")
    parser.add_argument("--cvd-filter", action="store_true", default=False,
                        help="Enable CVD since T_event filter (Phase C Option C)")
    parser.add_argument("--intra-window-watermark-threshold", type=float, default=None,
                        help="Phase D intra-window rolling high watermark threshold")
    parser.add_argument("--luld-proximity-threshold", type=float, default=None,
                        help="LULD proximity threshold (fraction, e.g. 0.02 = 2%% of upper band)")
    parser.add_argument("--luld-n-spread-multiple", type=float, default=None,
                        help="[DEPRECATED] Phase E spread multiple — use --luld-proximity-threshold")
    parser.add_argument("--luld-lower-disabled", action="store_true", default=False,
                        help="Phase F: disable lower-band LULD exit. EXIT_D owns downside.")
    parser.add_argument("--luld-exit-duration", type=float, default=None,
                        help="LULD V3: seconds bid must stay in proximity zone before EXIT_HALT fires (0=immediate)")
    return parser.parse_args()


def main():
    args = parse_args()
    # T7 unlock: test split guard removed. Runs exactly once, no iteration.

    results_dir = Path(args.results_dir) if args.results_dir else RESULTS_DIR
    results_dir.mkdir(parents=True, exist_ok=True)

    # ── Load phase config ──
    repo_root = Path(__file__).resolve().parent.parent
    config_path = (Path(args.config) if args.config
                   else repo_root / "config" / "strategy.json")
    with open(config_path) as f:
        phase_cfg = json.load(f)

    # CLI overrides (for sensitivity sweep)
    phase_label = phase_cfg.get("phase", "phase_a")
    exit_d_enabled = phase_cfg["exit_d"].get("enabled", True)
    exit_d_theta = (args.exit_d_theta
                    if args.exit_d_theta is not None
                    else phase_cfg["exit_d"]["theta"])
    exit_d_tau_min_sec = (args.exit_d_tau_min
                          if args.exit_d_tau_min is not None
                          else phase_cfg["exit_d"]["tau_min_sec"])
    gap_threshold = (args.gap_threshold
                     if args.gap_threshold is not None
                     else phase_cfg["gap_gate"]["threshold"])
    luld_cfg = phase_cfg["luld"]
    reentry_cfg = phase_cfg.get("reentry", {})
    reentry_enabled = reentry_cfg.get("enabled", False)
    reentry_tau_recovery_sec = reentry_cfg.get("tau_recovery_sec", 4.0)

    # Phase C backside filter params
    gap_gate_cfg = phase_cfg.get("gap_gate", {})
    gap_gate_enabled = gap_gate_cfg.get("enabled", True)  # True = legacy behavior
    if args.no_gap_gate:
        gap_gate_enabled = False
    watermark_cfg = phase_cfg.get("watermark_filter", {})
    watermark_threshold = (watermark_cfg.get("threshold")
                           if watermark_cfg.get("enabled", False) else None)
    if args.watermark_threshold is not None:
        watermark_threshold = args.watermark_threshold
    cvd_cfg = phase_cfg.get("cvd_filter", {})
    cvd_filter_enabled = cvd_cfg.get("enabled", False)
    if args.cvd_filter:
        cvd_filter_enabled = True

    # Phase D intra-window watermark
    intra_window_cfg = phase_cfg.get("intra_window_watermark", {})
    intra_window_watermark_threshold = (
        intra_window_cfg.get("threshold")
        if intra_window_cfg.get("enabled", False) else None
    )
    if args.intra_window_watermark_threshold is not None:
        intra_window_watermark_threshold = args.intra_window_watermark_threshold

    # LULD proximity threshold: canonical key is `proximity_threshold` (fraction).
    # Legacy fallback chain:
    #   proximity_pct_threshold (pct form, divide by 100) → n_spread_multiple (incompatible,
    #   warn and use default 0.02) → 0.02 hardcoded default.
    if "proximity_threshold" in luld_cfg:
        luld_proximity_threshold = float(luld_cfg["proximity_threshold"])
    elif "proximity_pct_threshold" in luld_cfg:
        luld_proximity_threshold = float(luld_cfg["proximity_pct_threshold"]) / 100.0
    elif "n_spread_multiple" in luld_cfg:
        log.warning(
            "luld.n_spread_multiple in config is incompatible with the new quote-based "
            "LULD module (bid-fraction threshold). Defaulting to proximity_threshold=0.02."
        )
        luld_proximity_threshold = 0.02
    else:
        luld_proximity_threshold = 0.02
    if getattr(args, "luld_proximity_threshold", None) is not None:
        luld_proximity_threshold = args.luld_proximity_threshold
    elif getattr(args, "luld_n_spread_multiple", None) is not None:
        log.warning(
            "--luld-n-spread-multiple is deprecated; use --luld-proximity-threshold. "
            "Treating the value as proximity_threshold directly."
        )
        luld_proximity_threshold = args.luld_n_spread_multiple

    # Phase F: lower-band gate. Default True (enabled) for backward compat with A-E configs.
    luld_lower_band_enabled = luld_cfg.get("lower_band_enabled", True)
    if args.luld_lower_disabled:
        luld_lower_band_enabled = False

    # V3: pin+duration clock — read from config, override with CLI
    luld_exit_duration_sec = float(luld_cfg.get("luld_exit_duration_sec", 0.0))
    if getattr(args, "luld_exit_duration", None) is not None:
        luld_exit_duration_sec = float(args.luld_exit_duration)

    log.info(
        f"Config: phase={phase_label} exit_d_enabled={exit_d_enabled} "
        f"theta={exit_d_theta:.2f} tau_min={exit_d_tau_min_sec:.1f}s "
        f"reentry_enabled={reentry_enabled} tau_recovery={reentry_tau_recovery_sec:.1f}s "
        f"gap_gate_enabled={gap_gate_enabled} gap={gap_threshold:.2f} "
        f"watermark_threshold={watermark_threshold} cvd_filter={cvd_filter_enabled} "
        f"intra_window_watermark={intra_window_watermark_threshold} "
        f"luld_proximity_threshold={luld_proximity_threshold:.4f} "
        f"luld_exit_duration_sec={luld_exit_duration_sec:.1f} "
        f"luld_lower_band_enabled={luld_lower_band_enabled}"
    )

    # ── Load hawkes + q_bar configs ──
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    phase_a_path = repo_root / "results" / "phase_a" / "production_fit_results.json"
    per_event_params = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            phase_a_results = json.load(f)
        for r in phase_a_results:
            if r.get("status") == "success" and "final_params" in r:
                per_event_params[(r["ticker"], r["date"])] = r["final_params"]

    # ── Load event catalog ──
    all_events = list_events(min_mom=50.0, require_date=True)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    if args.split == "train":
        events = [e for e in all_events if e["date"] < val_start]
    elif args.split == "val":
        events = [e for e in all_events if val_start <= e["date"] < test_start]
    elif args.split == "trainval":
        events = [e for e in all_events if e["date"] < test_start]
    elif args.split == "test":
        # T7 unlock: test split runs exactly once with Phase F config. No iteration.
        events = [e for e in all_events if e["date"] >= test_start]
    else:
        raise ValueError(f"Unknown split: {args.split}")

    # Ticker/date filter for smoke testing
    if args.ticker:
        events = [e for e in events if e["ticker"] == args.ticker]
    if args.date:
        events = [e for e in events if e["date"] == args.date]

    assert_split_valid([e["date"] for e in events], args.split, boundary)
    log.info(f"Split={args.split}: {len(events)} events after ticker/date filter")

    # ── Year-stratified random sampling (only when no ticker/date filter) ──
    if (args.random_sample and args.random_sample < len(events)
            and not args.ticker and not args.date):
        import random
        rng = random.Random(args.seed)
        n_sample = args.random_sample

        by_year: dict[str, list] = {}
        for e in events:
            year = e["date"][:4]
            by_year.setdefault(year, []).append(e)

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
        log.info(
            f"Stratified sample: {len(events)} events "
            f"(seed={args.seed}, allocation={dict(sorted(alloc.items()))})"
        )

    if args.max_events:
        events = events[:args.max_events]

    # ── Build work items ──
    exit_d_tau_min_ns = int(exit_d_tau_min_sec * NS_PER_SECOND)

    work_items = []
    for i, ev in enumerate(events):
        key = (ev["ticker"], ev["date"])
        fp = per_event_params.get(key, hawkes_median)
        work_items.append({
            "ticker": ev["ticker"],
            "date": ev["date"],
            "mom_pct": ev["mom_pct"],
            "event_idx": i,
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
            "gap_threshold": gap_threshold,
            "exit_d_enabled": exit_d_enabled,
            "exit_d_theta": exit_d_theta,
            "exit_d_tau_min_ns": exit_d_tau_min_ns,
            "luld_ref_window_sec": luld_cfg["ref_window_sec"],
            "luld_proximity_threshold": luld_proximity_threshold,
            "luld_exit_duration_sec": luld_exit_duration_sec,
            "luld_lower_band_enabled": luld_lower_band_enabled,
            "luld_warmup_sec": luld_cfg["warmup_sec"],
            "reentry_enabled": reentry_enabled,
            "reentry_tau_recovery_sec": reentry_tau_recovery_sec,
            "gap_gate_enabled": gap_gate_enabled,
            "watermark_threshold": watermark_threshold,
            "cvd_filter_enabled": cvd_filter_enabled,
            "intra_window_watermark_threshold": intra_window_watermark_threshold,
            "epg_cfg": phase_cfg.get("epg", {}),
        })

    log.info(
        f"Work items: {len(work_items)} | gap={gap_threshold} | "
        f"exit_d enabled={exit_d_enabled} theta={exit_d_theta} tau_min={exit_d_tau_min_sec}s"
    )

    # ── Process in parallel ──
    t0 = time.time()
    results: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    n_workers = min(args.workers, len(work_items)) if work_items else 1
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_process_event, item): item
                   for item in work_items}
        for future in as_completed(futures):
            item = futures[future]
            try:
                r = future.result()
            except Exception as e:
                r = {
                    "ticker": item["ticker"], "date": item["date"],
                    "event_idx": item["event_idx"],
                    "status": "error", "error": str(e),
                    "traceback": tb_module.format_exc(),
                }

            if r.get("status") == "event":
                results.append(r)
            elif r.get("status") == "skipped":
                skipped.append(r)
                log.info(f"skipped {r['ticker']} {r['date']}: {r.get('reason')}")
            else:
                errors.append(r)
                log.error(f"error {r['ticker']} {r['date']}: {r.get('error')}")

    elapsed = time.time() - t0
    log.info(
        f"Phase U complete: {len(results)} events with trades, "
        f"{len(skipped)} skipped, {len(errors)} errors in {elapsed:.1f}s"
    )

    # ── per_trade.parquet ──
    all_trades = []
    for ev in results:
        all_trades.extend(ev["trades"])

    if all_trades:
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
            tbl = pa.Table.from_pylist(all_trades)
            pq.write_table(tbl, str(results_dir / "per_trade.parquet"))
            log.info(
                f"Written: {results_dir / 'per_trade.parquet'} "
                f"({len(all_trades)} rows)"
            )
        except Exception as e:
            log.error(f"per_trade.parquet write failed: {e}")

    # ── blocked_edges.parquet (Phase D) ──
    if intra_window_watermark_threshold is not None:
        all_blocked = []
        for ev in results:
            all_blocked.extend(ev.get("blocked_edges", []))
        if all_blocked:
            try:
                import pyarrow as pa
                import pyarrow.parquet as pq
                be_tbl = pa.Table.from_pylist(all_blocked)
                pq.write_table(be_tbl, str(results_dir / "blocked_edges.parquet"))
                log.info(
                    f"Written: {results_dir / 'blocked_edges.parquet'} "
                    f"({len(all_blocked)} rows)"
                )
            except Exception as e:
                log.error(f"blocked_edges.parquet write failed: {e}")
        else:
            log.info("No blocked edges to write.")

    # ── per_event_summary.json ──
    per_event = []
    for ev in results:
        per_event.append(
            {k: v for k, v in ev.items()
             if k not in ("trades", "pass_window_durations", "blocked_edges")}
        )
    write_json_atomic(per_event, results_dir / "per_event_summary.json")

    # ── reentry_analysis.json (Phase B — skipped when reentry_enabled=False) ──
    if reentry_enabled and all_trades:
        reentry_counts = np.array(
            [ev.get("n_reentries_in_event", 0) for ev in results], dtype=np.float64
        )
        first_trades = [t for t in all_trades if t.get("entry_type", "first") == "first"]
        reentry_trades = [t for t in all_trades if t.get("entry_type") == "reentry"]

        def _trade_metrics(tlist):
            if not tlist:
                return {"n_trades": 0, "profit_factor": None, "win_rate": None,
                        "mean_pnl_pct": None, "mean_hold_sec": None,
                        "exit_reason_breakdown": {}}
            p = np.array([t["pnl_pct"] for t in tlist])
            w = p > 0; l = p < 0
            w_sum = float(np.sum(p[w])) if w.any() else 0.0
            l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
            by_ex = defaultdict(int)
            for t in tlist:
                by_ex[t["exit_reason"]] += 1
            return {
                "n_trades": len(tlist),
                "profit_factor": round(w_sum / l_sum, 4),
                "win_rate": round(float(np.mean(w)) * 100, 2),
                "mean_pnl_pct": round(float(np.mean(p)), 4),
                "mean_hold_sec": round(float(np.mean([t["hold_sec"] for t in tlist])), 2),
                "exit_reason_breakdown": dict(by_ex),
            }

        reentry_analysis = {
            "total_reentries": int(np.sum(reentry_counts)),
            "events_with_reentries": int(np.sum(reentry_counts > 0)),
            "mean_reentries_per_event": round(float(np.mean(reentry_counts)), 3),
            "median_reentries_per_event": float(np.median(reentry_counts)),
            "p25_reentries_per_event": float(np.percentile(reentry_counts, 25)),
            "p75_reentries_per_event": float(np.percentile(reentry_counts, 75)),
            "max_reentries_per_event": int(np.max(reentry_counts)),
            "reentry_count_distribution": [int(x) for x in reentry_counts.tolist()],
            "first_entry_metrics": _trade_metrics(first_trades),
            "reentry_metrics": _trade_metrics(reentry_trades),
        }
        write_json_atomic(reentry_analysis, results_dir / "reentry_analysis.json")
        log.info(
            f"Re-entry analysis: total={reentry_analysis['total_reentries']} "
            f"events_with_reentries={reentry_analysis['events_with_reentries']} "
            f"mean_per_event={reentry_analysis['mean_reentries_per_event']}"
        )

    # ── skipped_events.json ──
    if skipped or errors:
        skip_out = []
        for s in skipped:
            skip_out.append({"ticker": s["ticker"], "date": s["date"],
                             "reason": s.get("reason")})
        for e in errors:
            skip_out.append({"ticker": e["ticker"], "date": e["date"],
                             "reason": "error", "error": e.get("error")})
        write_json_atomic(skip_out, results_dir / "skipped_events.json")

    # ── run_summary.json ──
    summary = compute_run_summary(results)
    summary["meta"] = {
        "split": args.split,
        "random_sample": args.random_sample,
        "seed": args.seed,
        "gap_gate_enabled": gap_gate_enabled,
        "gap_threshold": gap_threshold,
        "watermark_threshold": watermark_threshold,
        "cvd_filter_enabled": cvd_filter_enabled,
        "intra_window_watermark_threshold": intra_window_watermark_threshold,
        "exit_d_theta": exit_d_theta,
        "exit_d_tau_min_sec": exit_d_tau_min_sec,
        "luld_proximity_threshold": luld_proximity_threshold,
        "luld_exit_duration_sec": luld_exit_duration_sec,
        "luld_lower_band_enabled": luld_lower_band_enabled,
        "n_events_input": len(events),
        "n_events_with_trades": len(results),
        "n_events_skipped": len(skipped),
        "n_events_errored": len(errors),
        "elapsed_sec": round(elapsed, 1),
    }
    write_json_atomic(summary, results_dir / "run_summary.json")

    # T2c assertion: luld_lower must be absent when lower band is disabled
    if not luld_lower_band_enabled:
        luld_lower_count = summary.get("exit_reason_breakdown", {}).get(
            "luld_lower", {}
        ).get("count", 0)
        assert luld_lower_count == 0, (
            f"BUG: luld_lower count={luld_lower_count} but lower_band_enabled=False"
        )
        log.info("T2c assertion passed: luld_lower count=0 with lower_band_enabled=False")

    log.info("=" * 70)
    log.info(
        f"PF={summary.get('profit_factor')} "
        f"n_trades={summary.get('n_trades')} "
        f"win%={summary.get('win_rate')} "
        f"mean_pnl%={summary.get('mean_pnl_pct')} "
        f"total_pnl%={summary.get('total_pnl_pct')}"
    )
    log.info("=" * 70)

    # ── summary.json (phase spec output) ──
    phase_summary = {
        "phase": phase_label,
        "n_events_input": len(events),
        "n_events_with_trades": len(results),
        "n_events_skipped": len(skipped),
        "n_events_errored": len(errors),
        "n_trades": summary.get("n_trades", 0),
        "profit_factor": summary.get("profit_factor"),
        "win_rate": summary.get("win_rate"),
        "mean_pnl_pct": summary.get("mean_pnl_pct"),
        "median_pnl_pct": summary.get("median_pnl_pct"),
        "mean_hold_sec": summary.get("mean_hold_sec"),
        "exit_d_enabled": exit_d_enabled,
        "exit_reason_breakdown": summary.get("exit_reason_breakdown", {}),
        "meta": summary.get("meta", {}),
    }
    write_json_atomic(phase_summary, results_dir / "summary.json")
    log.info(f"Written: {results_dir / 'summary.json'}")

    # ── trade_log.json (phase spec output) ──
    trade_log = [
        {
            "ticker": t["ticker"],
            "date": t["date"],
            "event_idx": t["event_idx"],
            "entry_ts": t["entry_ts"],
            "exit_ts": t["exit_ts"],
            "exit_reason": t["exit_reason"],
            "pnl_pct": t["pnl_pct"],
            "hold_sec": t["hold_sec"],
        }
        for t in all_trades
    ]
    write_json_atomic(trade_log, results_dir / "trade_log.json")
    log.info(f"Written: {results_dir / 'trade_log.json'} ({len(trade_log)} records)")


if __name__ == "__main__":
    main()
