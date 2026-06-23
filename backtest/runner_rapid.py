#!/usr/bin/env python3
"""EPG-Rapid runner — halt-aware Hawkes, configurable entry, EPG-close-only exit.

Operating modes
---------------
parity  (--parity):
    Delegates each event to runner._process_event for T3 verification.
    Produces output identical to runner.py when run with matching config.
    Use: compare parity_diff.json — must be empty before R1.

rapid (default, --entry-mode {rising_edge|first_pass|cross_and_hold}):
    Halt windows detected via detect_luld_halts() and fed to Hawkes replay.
    Exit stack: EPG window close only (EXIT_D off, LULD off).
    Hard re-entry off: closed_today=True after first entry; one trade per event.

Results layout
--------------
Old invalidated results (pre-scanner-floor-fix):  results/phase_r0/
New MDR>=150 diagnostic results:                   results/phase_r0_mdr150/

T3 parity check:
    python -m backtest.runner_rapid --parity \\
        --split val --results-dir results/phase_r0_mdr150/parity

T4 gate-consistent baseline (classic first-PASS on MDR>=150):
    python -m backtest.runner_rapid --entry-mode rising_edge \\
        --event-file data/val_mdr150_diagnostic.json \\
        --split val --results-dir results/phase_r0_mdr150/baseline_mdr150

T5 rapid first-PASS on MDR>=150 (EPG-Rapid design):
    python -m backtest.runner_rapid --entry-mode first_pass \\
        --event-file data/val_mdr150_diagnostic.json \\
        --split val --results-dir results/phase_r0_mdr150/rapid_mdr150

R1+ asymmetric gate sweep:
    python -m backtest.runner_rapid --entry-mode first_pass \\
        --event-file data/val_mdr150_diagnostic.json \\
        --p-open 0.70 --p-close 0.65 \\
        --split val --results-dir results/phase_r1_mdr150/rapid_po70_pc65
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
from typing import Optional

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))

from data.schemas.mom_db import CONFIG_DIR, DATA_ROOT, NS_PER_SECOND
from data.loaders.trades import (
    load_trades, list_events, _session_ns_bounds,
    compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from backtest.setup_filter import run_setup_filter, _build_1min_bars
from core.hawkes.engine import hawkes_replay_fixed_beta
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online, HawkesParams
from core.filters.rapid_entry import Q_THRESHOLD
from core.features.luld_halt_detection import detect_luld_halts


# ── Constants ──────────────────────────────────────────────────────────

PHASE_NAME = "epg_rapid"
RESULTS_DIR = Path(__file__).resolve().parent / "results" / "phase_r0"
LOG_DIR = Path(__file__).resolve().parent / "logs"

EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0

RTH_START_SEC = 19800.0
RTH_END_SEC   = 43200.0

REFIT_INTERVAL = 50
COLD_START_SIZE = 1000
REFIT_WINDOW = 10000
BETA_FIXED = 0.1

HALT_GAP_THRESHOLD = 60.0


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


# ── Split assertion ────────────────────────────────────────────────────

def assert_split_valid(event_dates: list[str], split: str, boundary: dict) -> None:
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    if split == "val":
        violations = [d for d in event_dates if d < val_start or d >= test_start]
    elif split == "train":
        violations = [d for d in event_dates if d >= val_start]
    elif split == "trainval":
        violations = [d for d in event_dates if d >= test_start]
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
# Self-contained copy (runner.py is read-only). Includes C3 halt-gap pause.
# When halt_intervals=None this is 100% identical to the original pre-C3 code.

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
    halt_gap_threshold: float = HALT_GAP_THRESHOLD,
) -> "HawkesParams | None":
    """Replay Hawkes with online MLE refitting every refit_interval trades.

    halt_intervals: list of (start_sec, end_sec) in the same frame as t_sec.
    None (default) is 100% identical to original behavior.
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


# ── Halt window conversion ─────────────────────────────────────────────

def _build_halt_intervals(td) -> list:
    """Detect LULD-style halts and return [(start_sec, end_sec)] in t_sec frame.

    t_sec frame: seconds since first trade (same as td.t_sec[0] == 0.0).
    Returns [] when no qualifying halt windows are found or on any error.
    """
    try:
        trades_df = pd.DataFrame(
            {"price": td.prices},
            index=pd.to_datetime(td.timestamps, unit="ns"),
        )
        halt_windows = detect_luld_halts(trades_df, price_col="price")
        if not halt_windows:
            return []
        t0_ns = int(td.timestamps[0])
        return [
            (
                (hw.start.value - t0_ns) / NS_PER_SECOND,
                (hw.end.value - t0_ns) / NS_PER_SECOND,
            )
            for hw in halt_windows
        ]
    except Exception:
        return []


# ══════════════════════════════════════════════════════════════════════
#  WORKER — Process one event
# ══════════════════════════════════════════════════════════════════════


def _process_event_rapid(args: dict) -> dict:
    """Run EPG-Rapid backtest on one event.

    Parity mode (args["parity"] == True): delegates to runner._process_event,
    producing output identical to runner.py for T3 diff verification.

    Rapid mode: halt-aware Hawkes, configurable entry, EPG-close-only exit.
    """
    if args.get("parity"):
        # T3 parity: delegate entirely to the classic runner.
        # Lazy import to avoid runner.py's module-level logging in rapid mode.
        import runner as _r
        return _r._process_event(args)

    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    event_idx = args["event_idx"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    entry_mode = args.get("entry_mode", "cross_and_hold")
    n_hold = args.get("n_hold", 3)
    roc_min = args.get("roc_min", None)
    gap_gate_enabled = args.get("gap_gate_enabled", False)
    gap_threshold = args.get("gap_threshold", 0.30)
    epg_cfg = args.get("epg_cfg", {})
    gate_mode = epg_cfg.get("gate_mode", "peak")
    epg_tau_peak = epg_cfg.get("tau_peak", 600.0)
    epg_C = epg_cfg.get("C", 2.0)
    epg_p_open = float(epg_cfg.get("p_open", EPG_P))
    epg_p_close = float(epg_cfg.get("p_close", epg_p_open))

    base = {"ticker": ticker, "date": date, "event_idx": event_idx}

    try:
        # ── 1. Load data ──
        td = load_trades(ticker, date, mom_pct)
        if td is None or td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        prev_close = get_prev_close(ticker, date)
        if prev_close is None or prev_close <= 0:
            return {**base, "status": "skipped", "reason": "missing_prev_close"}

        # ── 1b. Setup filter ──
        start_ns, end_ns = _session_ns_bounds(date)
        sf = run_setup_filter(
            timestamps=td.timestamps,
            prices=td.prices,
            sizes=td.sizes,
            session_start_ns=start_ns,
            session_end_ns=end_ns,
        )
        # Bar starts for bar-aware entry_eligible check (separate call — run_setup_filter
        # does not expose bar_starts from its internal _build_1min_bars invocation)
        _, _, _, _, _, _, bar_starts_sf = _build_1min_bars(
            td.timestamps, td.prices, td.sizes.astype(np.int64), start_ns, end_ns
        )
        N = td.n_trades

        # ── Scanner hit floor ──
        # If event is in the catalog and price never reached the scanner threshold,
        # we would never trade it live → skip.  If the catalog has a hit timestamp,
        # no tick before that timestamp is eligible for entry.
        _sh_ts_ns       = args.get("scanner_hit_ts_ns")          # int | None
        _sh_in_catalog  = args.get("scanner_hit_in_catalog", False)
        scanner_hit_t_sec: float | None = None
        if _sh_in_catalog:
            if _sh_ts_ns is None:
                return {**base, "status": "skipped", "reason": "no_scanner_hit"}
            scanner_hit_t_sec = (_sh_ts_ns - int(td.timestamps[0])) / NS_PER_SECOND

        gate_at_scanner_hit: str | None = None

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

        # ── 3. Halt windows for Hawkes clock pause ──
        halt_intervals = _build_halt_intervals(td)

        # ── 4. Hawkes replay with online refit ──
        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)
        dv_arr = td.prices.astype(np.float64) * td.sizes.astype(np.float64)

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
            halt_intervals=halt_intervals or None,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # ── 5. EPG ──
        global_lref_epg = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=global_lref_epg, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)
        gate = ParticipationGate(
            half_life_seconds=EPG_TAU,
            peak_threshold_p=epg_p_open,
            warmup_seconds=EPG_WARMUP,
            gate_mode=gate_mode,
            tau_peak=epg_tau_peak,
            C=epg_C,
            p_open=epg_p_open,
            p_close=epg_p_close,
        )

        epg_states = [GateState.INACTIVE] * N
        t_event_fired = False
        t_event_idx = None
        t_event_sec = 0.0
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None and not t_event_fired:
                gate.activate(t_ev)
                t_event_fired = True
                t_event_idx = i
                t_event_sec = td.t_sec[i]
            dv = float(td.prices[i]) * float(td.sizes[i])
            epg_states[i] = gate.update(dv, td.t_sec[i])
            if (scanner_hit_t_sec is not None and gate_at_scanner_hit is None
                    and td.t_sec[i] >= scanner_hit_t_sec):
                gate_at_scanner_hit = epg_states[i].name

        if not t_event_fired:
            return {**base, "status": "skipped", "reason": "no_t_event",
                    "n_trades_in_event": N}

        # ── 6. Entry/exit state machine (EPG-Rapid) ──
        # Exit stack: EPG window close only.
        # Entry state machine. Hard re-entry off: closed_today=True after first entry.
        # rising_edge (classic): FAIL→PASS rising edge only. NEVER calls entry_eligible.
        # cross_and_hold: any PASS tick, gated by n_hold bars of entry_eligible.
        session_start_ns, _ = _session_ns_bounds(date)

        trades = []
        in_position = False
        closed_today = False
        entry_idx = None
        entry_price = None
        entry_t_sec = None
        intraday_pct_at_entry = None
        prev_state = GateState.INACTIVE
        n_pass_edges = 0
        n_entry_eligible_blocks = 0
        n_gap_gate_blocks = 0
        n_passtofail = 0  # total PASS→not-PASS transitions across entire event
        pass_window_start_sec = None
        pass_window_durations: list[float] = []

        for i in range(N):
            cur = epg_states[i]

            # Scanner hit floor: no entry processing before first scanner hit tick
            if scanner_hit_t_sec is not None and td.t_sec[i] < scanner_hit_t_sec:
                prev_state = cur
                continue

            if prev_state == GateState.PASS and cur != GateState.PASS:
                n_passtofail += 1
                if pass_window_start_sec is not None:
                    pass_window_durations.append(td.t_sec[i] - pass_window_start_sec)
                    pass_window_start_sec = None
            elif cur == GateState.PASS and prev_state != GateState.PASS:
                pass_window_start_sec = td.t_sec[i]

            if not in_position and not closed_today:
                entry_accepted = False

                if entry_mode == "rising_edge":
                    # Classic: rising-edge only; zero SF involvement (§1.1 constraint)
                    if (cur == GateState.PASS and
                            prev_state in (GateState.INACTIVE, GateState.WARMUP,
                                           GateState.FAIL)):
                        n_pass_edges += 1
                        entry_accepted = True
                elif entry_mode == "first_pass":
                    # First-PASS (EPG-Rapid §1): enter on first live PASS tick.
                    # No rising-edge requirement, no SF, no entry_eligible(), no n_hold.
                    if cur == GateState.PASS:
                        entry_accepted = True
                else:  # cross_and_hold: any PASS tick, gated by entry_eligible
                    if cur == GateState.PASS:
                        _bar_idx = max(0, int(np.searchsorted(
                            bar_starts_sf, td.timestamps[i], side="right")) - 1)
                        _q_at_entry = sf.q_tilde[:_bar_idx + 1]
                        if (len(_q_at_entry) < n_hold or
                                not bool(np.all(_q_at_entry[-n_hold:] >= Q_THRESHOLD))):
                            n_entry_eligible_blocks += 1
                        else:
                            entry_accepted = True

                if entry_accepted:
                    # Gap gate check (optional, off by default in rapid mode)
                    cur_price = float(td.prices[i])
                    if gap_gate_enabled:
                        intraday_pct = (cur_price - prev_close) / prev_close
                        if intraday_pct < gap_threshold:
                            n_gap_gate_blocks += 1
                            prev_state = cur
                            continue
                    else:
                        intraday_pct = (cur_price - prev_close) / prev_close

                    entry_price = float(td.prices[min(i + 1, N - 1)])
                    entry_idx = i
                    entry_t_sec = td.t_sec[i]
                    intraday_pct_at_entry = intraday_pct
                    in_position = True
                    closed_today = True  # hard re-entry off

            elif in_position:
                # EPG window close: PASS → not-PASS
                if prev_state == GateState.PASS and cur != GateState.PASS:
                    exit_price = float(td.prices[min(i + 1, N - 1)])
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
                        "entry_lag_sec": float(entry_t_sec - t_event_sec),
                        "entry_lag_from_scanner_sec": (
                            float(entry_t_sec - scanner_hit_t_sec)
                            if scanner_hit_t_sec is not None else None
                        ),
                        "entry_price": float(entry_price),
                        "exit_price": float(exit_price),
                        "pnl_pct": float(pnl_pct),
                        "intraday_pct_at_entry": float(intraday_pct_at_entry),
                        "prev_close": float(prev_close),
                        "time_of_day_sec": float(tod_sec),
                        "session_bucket": session_bucket(tod_sec),
                        "exit_reason": "epg_window_close",
                        "n_halt_windows": len(halt_intervals),
                    })
                    in_position = False
                    entry_idx = entry_price = entry_t_sec = None
                    intraday_pct_at_entry = None
                    # closed_today stays True — no re-entry

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
                "entry_lag_sec": float(entry_t_sec - t_event_sec),
                "entry_lag_from_scanner_sec": (
                    float(entry_t_sec - scanner_hit_t_sec)
                    if scanner_hit_t_sec is not None else None
                ),
                "entry_price": float(entry_price),
                "exit_price": float(exit_price),
                "pnl_pct": float(pnl_pct),
                "intraday_pct_at_entry": float(intraday_pct_at_entry),
                "prev_close": float(prev_close),
                "time_of_day_sec": float(tod_sec),
                "session_bucket": session_bucket(tod_sec),
                "exit_reason": "session_end",
                "n_halt_windows": len(halt_intervals),
            })

        # Close any open PASS window at event end
        if pass_window_start_sec is not None:
            pass_window_durations.append(td.t_sec[N - 1] - pass_window_start_sec)

        # Hard assertion: rising_edge and first_pass must never call entry_eligible
        if entry_mode in ("rising_edge", "first_pass") and n_entry_eligible_blocks != 0:
            raise AssertionError(
                f"[§1.1] {entry_mode} mode called entry_eligible {n_entry_eligible_blocks}x"
                f" in {ticker} {date} — code bug, entry paths not separated"
            )

        return {
            **base,
            "status": "event",
            "trades": trades,
            "n_trades_in_event": int(len(trades)),
            "n_pass_edges": int(n_pass_edges),
            "n_entry_eligible_blocks": int(n_entry_eligible_blocks),
            "n_gap_gate_blocks": int(n_gap_gate_blocks),
            "n_halt_windows": int(len(halt_intervals)),
            "n_event_trades": int(N),
            "n_passtofail_transitions": int(n_passtofail),
            "pass_window_durations": pass_window_durations,
            "gate_at_scanner_hit": gate_at_scanner_hit,
            "prev_close": float(prev_close),
            "first_price": float(td.prices[0]),
            "last_price": float(td.prices[-1]),
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

    wins = pnl > 0
    losses = pnl < 0
    win_sum = float(np.sum(pnl[wins])) if wins.any() else 0.0
    loss_sum = float(np.abs(np.sum(pnl[losses]))) if losses.any() else 1e-10
    pf = win_sum / loss_sum

    by_session = defaultdict(list)
    for t in all_trades:
        by_session[t["session_bucket"]].append(t)
    session_breakdown = {}
    for bucket, trades_b in by_session.items():
        p = np.array([t["pnl_pct"] for t in trades_b])
        w = p > 0
        l = p < 0
        w_sum = float(np.sum(p[w])) if w.any() else 0.0
        l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
        session_breakdown[bucket] = {
            "n_trades": len(trades_b),
            "profit_factor": round(w_sum / l_sum, 4),
            "mean_pnl_pct": round(float(np.mean(p)), 4),
            "win_rate": round(float(np.mean(w)) * 100, 2),
        }

    by_exit = defaultdict(list)
    for t in all_trades:
        by_exit[t["exit_reason"]].append(t)
    exit_breakdown = {}
    for reason, trades_e in by_exit.items():
        p = np.array([t["pnl_pct"] for t in trades_e])
        w = p > 0
        l = p < 0
        w_sum = float(np.sum(p[w])) if w.any() else 0.0
        l_sum = float(np.abs(np.sum(p[l]))) if l.any() else 1e-10
        exit_breakdown[reason] = {
            "count": len(trades_e),
            "pct_of_trades": round(100 * len(trades_e) / n_trades, 2),
            "profit_factor": round(w_sum / l_sum, 4),
            "mean_pnl_pct": round(float(np.mean(p)), 4),
        }

    sorted_pnl = np.sort(pnl)
    cvar_n = max(1, int(0.05 * n_trades))
    cvar5 = float(np.mean(sorted_pnl[:cvar_n]))

    n_halt_windows = sum(
        ev.get("n_halt_windows", 0) for ev in events if ev.get("status") == "event"
    )

    entry_lags = [t.get("entry_lag_sec") for t in all_trades
                  if t.get("entry_lag_sec") is not None]
    mean_entry_lag_sec = round(float(np.mean(entry_lags)), 2) if entry_lags else None

    scanner_lags = [t.get("entry_lag_from_scanner_sec") for t in all_trades
                    if t.get("entry_lag_from_scanner_sec") is not None]
    mean_entry_lag_from_scanner_sec = (
        round(float(np.mean(scanner_lags)), 2) if scanner_lags else None
    )
    median_entry_lag_from_scanner_sec = (
        round(float(np.median(scanner_lags)), 2) if scanner_lags else None
    )
    p90_entry_lag_from_scanner_sec = (
        round(float(np.percentile(scanner_lags, 90)), 2) if scanner_lags else None
    )

    gate_states_at_scanner = [
        ev.get("gate_at_scanner_hit") for ev in events
        if ev.get("status") == "event" and ev.get("gate_at_scanner_hit") is not None
    ]
    n_pass_at_scanner = sum(1 for s in gate_states_at_scanner if s == "PASS")
    pct_events_gate_pass_at_scanner_hit = (
        round(100.0 * n_pass_at_scanner / len(gate_states_at_scanner), 2)
        if gate_states_at_scanner else None
    )

    n_events_total = sum(1 for ev in events if ev.get("status") == "event")
    total_passtofail = sum(
        ev.get("n_passtofail_transitions", 0)
        for ev in events if ev.get("status") == "event"
    )
    mean_passtofail_per_event = (
        round(total_passtofail / n_events_total, 3) if n_events_total > 0 else None
    )

    all_pass_windows: list[float] = []
    for ev in events:
        if ev.get("status") == "event":
            all_pass_windows.extend(ev.get("pass_window_durations", []))
    mean_consecutive_pass_window_sec = (
        round(float(np.mean(all_pass_windows)), 2) if all_pass_windows else None
    )

    return {
        "n_trades": int(n_trades),
        "profit_factor": round(pf, 4),
        "win_rate": round(float(np.mean(wins)) * 100, 2),
        "mean_pnl_pct": round(float(np.mean(pnl)), 4),
        "median_pnl_pct": round(float(np.median(pnl)), 4),
        "total_pnl_pct": round(float(np.sum(pnl)), 4),
        "cvar5_pct": round(cvar5, 4),
        "max_win_pct": round(float(np.max(pnl)), 4),
        "max_loss_pct": round(float(np.min(pnl)), 4),
        "mean_hold_sec": round(float(np.mean(hold)), 2),
        "median_hold_sec": round(float(np.median(hold)), 2),
        "mean_entry_lag_sec": mean_entry_lag_sec,
        "mean_entry_lag_from_scanner_sec": mean_entry_lag_from_scanner_sec,
        "median_entry_lag_from_scanner_sec": median_entry_lag_from_scanner_sec,
        "p90_entry_lag_from_scanner_sec": p90_entry_lag_from_scanner_sec,
        "pct_events_gate_pass_at_scanner_hit": pct_events_gate_pass_at_scanner_hit,
        "mean_passtofail_per_event": mean_passtofail_per_event,
        "mean_consecutive_pass_window_sec": mean_consecutive_pass_window_sec,
        "session_breakdown": session_breakdown,
        "exit_reason_breakdown": exit_breakdown,
        "n_events_with_halt_windows": int(n_halt_windows),
    }


# ── Parity diff ────────────────────────────────────────────────────────

def compute_parity_diff(
    classic_events: list[dict],
    parity_events: list[dict],
) -> dict:
    """Compare trade-level output of classic runner vs parity mode.

    Returns {"n_diffs": 0, "diffs": []} on clean parity.
    """
    def _key(t):
        return (t["ticker"], t["date"], t["trade_seq"])

    classic_trades = {}
    for ev in classic_events:
        if ev.get("status") == "event":
            for t in ev["trades"]:
                classic_trades[_key(t)] = t

    parity_trades = {}
    for ev in parity_events:
        if ev.get("status") == "event":
            for t in ev["trades"]:
                parity_trades[_key(t)] = t

    compare_fields = [
        "entry_ts", "exit_ts", "entry_price", "exit_price",
        "pnl_pct", "hold_sec", "exit_reason",
    ]
    diffs = []

    all_keys = set(classic_trades) | set(parity_trades)
    for k in sorted(all_keys):
        ct = classic_trades.get(k)
        pt = parity_trades.get(k)
        if ct is None:
            diffs.append({"key": k, "issue": "missing_in_classic"})
        elif pt is None:
            diffs.append({"key": k, "issue": "missing_in_parity"})
        else:
            field_diffs = {}
            for f in compare_fields:
                cv, pv = ct.get(f), pt.get(f)
                if isinstance(cv, float) and isinstance(pv, float):
                    if abs(cv - pv) > 1e-9:
                        field_diffs[f] = {"classic": cv, "parity": pv}
                elif cv != pv:
                    field_diffs[f] = {"classic": cv, "parity": pv}
            if field_diffs:
                diffs.append({"key": k, "field_diffs": field_diffs})

    return {
        "n_classic_trades": len(classic_trades),
        "n_parity_trades": len(parity_trades),
        "n_diffs": len(diffs),
        "diffs": diffs[:50],  # cap at 50 for readability
    }


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════


def parse_args():
    parser = argparse.ArgumentParser(description="EPG-Rapid Runner")
    parser.add_argument("--split", type=str, default="val",
                        choices=["train", "val", "trainval"],
                        help="Split to run on (test locked until R5)")
    parser.add_argument("--parity", action="store_true", default=False,
                        help="T3 parity mode: delegate to runner._process_event")
    parser.add_argument("--entry-mode", type=str, default="first_pass",
                        choices=["rising_edge", "first_pass", "cross_and_hold"],
                        help="Entry trigger: rising_edge (baseline), first_pass (EPG-Rapid), cross_and_hold (legacy)")
    parser.add_argument("--n-hold", type=int, default=3,
                        help="entry_eligible n_hold bars (default 3)")
    parser.add_argument("--roc-min", type=float, default=None,
                        help="Minimum 5-min ROC to enter (None = disabled)")
    parser.add_argument("--no-gap-gate", action="store_true", default=False,
                        help="Disable gap gate (default in rapid mode)")
    parser.add_argument("--gap-threshold", type=float, default=0.30)
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--random-sample", type=int, default=100)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--results-dir", type=str, default=None)
    parser.add_argument("--ticker", type=str, default=None)
    parser.add_argument("--date", type=str, default=None)
    # Parity mode options (forwarded to runner._process_event)
    parser.add_argument("--luld-lower-disabled", action="store_true", default=False,
                        help="Parity: pass luld_lower_band_enabled=False to runner")
    parser.add_argument("--p-open", type=float, default=None,
                        help="EPG gate open threshold (default: 0.65 from EPG_P)")
    parser.add_argument("--p-close", type=float, default=None,
                        help="EPG gate close threshold (default: same as --p-open)")
    parser.add_argument("--event-file", type=str, default=None,
                        help="Path to pre-built event sample JSON (e.g. val_mdr150_diagnostic.json). "
                             "Bypasses list_events() and stratified sampling entirely.")
    return parser.parse_args()


def main():
    args = parse_args()

    results_dir = (
        Path(args.results_dir) if args.results_dir
        else RESULTS_DIR / ("parity" if args.parity else "baseline")
    )
    results_dir.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parent.parent
    config_path = (Path(args.config) if args.config
                   else Path(__file__).resolve().parent / "config" / "strategy.json")
    with open(config_path) as f:
        phase_cfg = json.load(f)

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

    # ── Scanner hit catalog (pre-computed floor timestamps) ──
    scanner_hit_catalog: dict = {}
    catalog_path = DATA_ROOT / "filtered" / "scanner_hit_catalog.json"
    if catalog_path.exists():
        with open(catalog_path) as f:
            scanner_hit_catalog = json.load(f)
        log.info(f"Scanner hit catalog: {len(scanner_hit_catalog)} entries loaded")
    else:
        log.warning(f"Scanner hit catalog not found at {catalog_path} — floor guard disabled")

    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]

    if args.event_file:
        # Load pre-built sample (e.g. MDR>=150 diagnostic) — bypass list_events + sampling.
        with open(args.event_file) as f:
            ef = json.load(f)
        events = ef["events"]
        if args.ticker:
            events = [e for e in events if e["ticker"] == args.ticker]
        if args.date:
            events = [e for e in events if e["date"] == args.date]
        assert_split_valid([e["date"] for e in events], args.split, boundary)
        log.info(f"Event file {args.event_file}: {len(events)} events loaded")
    else:
        all_events = list_events(min_mom=50.0, require_date=True)
        if args.split == "train":
            events = [e for e in all_events if e["date"] < val_start]
        elif args.split == "val":
            events = [e for e in all_events if val_start <= e["date"] < test_start]
        elif args.split == "trainval":
            events = [e for e in all_events if e["date"] < test_start]
        else:
            raise ValueError(f"Unknown split: {args.split}")

        if args.ticker:
            events = [e for e in events if e["ticker"] == args.ticker]
        if args.date:
            events = [e for e in events if e["date"] == args.date]

        assert_split_valid([e["date"] for e in events], args.split, boundary)
        log.info(f"Split={args.split}: {len(events)} events after filter")

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
                f"(seed={args.seed}, alloc={dict(sorted(alloc.items()))})"
            )

    if args.max_events:
        events = events[:args.max_events]

    # ── Build work items ──
    luld_cfg = phase_cfg["luld"]

    # LULD proximity threshold (for parity mode pass-through)
    if "proximity_threshold" in luld_cfg:
        luld_proximity_threshold = float(luld_cfg["proximity_threshold"])
    elif "proximity_pct_threshold" in luld_cfg:
        luld_proximity_threshold = float(luld_cfg["proximity_pct_threshold"]) / 100.0
    else:
        luld_proximity_threshold = 0.02

    luld_lower_band_enabled = luld_cfg.get("lower_band_enabled", True)
    if args.luld_lower_disabled:
        luld_lower_band_enabled = False

    exit_d_cfg = phase_cfg.get("exit_d", {})
    exit_d_enabled = exit_d_cfg.get("enabled", False)
    exit_d_theta = exit_d_cfg.get("theta", 0.65)
    exit_d_tau_min_ns = int(exit_d_cfg.get("tau_min_sec", 4.0) * NS_PER_SECOND)

    gap_gate_cfg = phase_cfg.get("gap_gate", {})
    if args.parity:
        # Parity: delegate gap-gate control to strategy.json to match runner.py exactly.
        gap_gate_enabled = gap_gate_cfg.get("enabled", True)
        gap_threshold = gap_gate_cfg.get("threshold", args.gap_threshold)
    else:
        # Rapid: gap gate OFF by default (scanner already ensures >=30%).
        gap_gate_enabled = False
        gap_threshold = args.gap_threshold

    work_items = []
    for i, ev in enumerate(events):
        key = (ev["ticker"], ev["date"])
        fp = per_event_params.get(key, hawkes_median)
        catalog_key = f"{ev['ticker']}:{ev['date']}"
        catalog_rec = scanner_hit_catalog.get(catalog_key)  # None if not in catalog
        item = {
            "ticker": ev["ticker"],
            "date": ev["date"],
            "mom_pct": ev["mom_pct"],
            "event_idx": i,
            "hawkes_params": fp,
            "rho": hawkes_median.get("rho", 0.99),
            "rho_E": hawkes_median.get("rho", 0.99),
            "q_bar_cfg": q_bar_cfg,
            "epg_cfg": {
                **phase_cfg.get("epg", {}),
                **({"p_open": args.p_open} if args.p_open is not None else {}),
                **({"p_close": args.p_close} if args.p_close is not None else
                   ({"p_close": args.p_open} if args.p_open is not None else {})),
            },
            # rapid-mode params
            "parity": args.parity,
            "entry_mode": args.entry_mode,
            "n_hold": args.n_hold,
            "roc_min": args.roc_min,
            "gap_gate_enabled": gap_gate_enabled,
            "gap_threshold": gap_threshold,
            # parity pass-through (runner._process_event keys)
            "exit_d_enabled": exit_d_enabled,
            "exit_d_theta": exit_d_theta,
            "exit_d_tau_min_ns": exit_d_tau_min_ns,
            "luld_ref_window_sec": luld_cfg["ref_window_sec"],
            "luld_proximity_threshold": luld_proximity_threshold,
            "luld_exit_duration_sec": float(luld_cfg.get("luld_exit_duration_sec", 0.0)),
            "luld_lower_band_enabled": luld_lower_band_enabled,
            "luld_warmup_sec": luld_cfg["warmup_sec"],
            "reentry_enabled": phase_cfg.get("reentry", {}).get("enabled", False),
            "reentry_tau_recovery_sec": phase_cfg.get("reentry", {}).get("tau_recovery_sec", 4.0),
            "watermark_threshold": None,
            "cvd_filter_enabled": False,
            "intra_window_watermark_threshold": None,
            # Scanner hit floor
            "scanner_hit_ts_ns": catalog_rec.get("scanner_hit_ts_ns") if catalog_rec else None,
            "scanner_hit_in_catalog": catalog_rec is not None,
        }
        work_items.append(item)

    if args.parity:
        mode_label = "parity"
    elif args.entry_mode == "rising_edge":
        mode_label = "rapid/rising_edge"
    elif args.entry_mode == "first_pass":
        mode_label = "rapid/first_pass"
    else:
        mode_label = f"rapid/cross_and_hold/n_hold={args.n_hold}"
    log.info(f"Mode={mode_label} | {len(work_items)} events")

    # ── Process in parallel ──
    t0 = time.time()
    results: list[dict] = []
    skipped: list[dict] = []
    errors: list[dict] = []

    n_workers = min(args.workers, len(work_items)) if work_items else 1
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_process_event_rapid, item): item
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
        f"{mode_label} complete: {len(results)} events, "
        f"{len(skipped)} skipped, {len(errors)} errors in {elapsed:.1f}s"
    )

    # ── Write per_event_summary.json ──
    per_event = [
        {k: v for k, v in ev.items() if k not in ("trades", "pass_window_durations")}
        for ev in results
    ]
    write_json_atomic(per_event, results_dir / "per_event_summary.json")

    # ── Write per_trade.json ──
    all_trades = []
    for ev in results:
        all_trades.extend(ev.get("trades", []))
    write_json_atomic(all_trades, results_dir / "per_trade.json")

    # ── Compute and write run summary ──
    summary = compute_run_summary(results)
    summary["run_config"] = {
        "mode": mode_label,
        "entry_mode": args.entry_mode,
        "n_hold": args.n_hold,
        "p_open": args.p_open if args.p_open is not None else EPG_P,
        "p_close": (args.p_close if args.p_close is not None
                    else (args.p_open if args.p_open is not None else EPG_P)),
        "roc_min": args.roc_min,
        "n_events_sampled": len(events),
        "seed": args.seed,
        "split": args.split,
    }
    write_json_atomic(summary, results_dir / "run_summary.json")

    log.info(
        f"Summary: n_trades={summary.get('n_trades')} "
        f"PF={summary.get('profit_factor')} "
        f"win%={summary.get('win_rate')} "
        f"CVaR5={summary.get('cvar5_pct')}"
    )

    # ── Escalation checks ──
    pf = summary.get("profit_factor")
    if not args.parity and pf is not None and pf < 1.00:
        log.error(
            f"ESCALATION: PF={pf:.4f} < 1.00 hard-stop threshold (MDR>=150 sample). "
            "Do not proceed to R1."
        )

    if errors:
        log.warning(f"{len(errors)} events errored. First: {errors[0].get('error')}")

    log.info(f"Results written to {results_dir}")
    return summary


if __name__ == "__main__":
    main()
