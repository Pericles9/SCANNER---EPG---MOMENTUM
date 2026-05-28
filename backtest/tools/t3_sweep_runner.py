"""
T3 — Full 129-config parameter sweep on the 300-event training sample.

Runs all gate variants in order A → B → C → D → E. Writes intermediate
results after each variant completes for recoverability.

All runs:
  - EXIT_D disabled
  - LULD disabled
  - Re-entry disabled (max one trade per PASS window; multiple windows per event allowed)
  - Gap gate disabled (entry on every PASS rising edge)
  - Intra-window watermark disabled
  - EPG window close is the only exit

Sweep grid (129 configs total):
  Variant A (ParticipationGate + hysteresis):  72 combos
  Variant B (AbsoluteThresholdGate):            7 combos
  Variant C (HawkesCumulativeGate):            15 combos
  Variant D (HawkesBuySideGate):               15 combos
  Variant E (BurstRatioGate):                  20 combos

Output files:
  results/phase_epg_grt/sweep/variant_a.json … variant_e.json
  results/phase_epg_grt/sweep/all_configs.json
"""
from __future__ import annotations

import json
import logging
import math
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import list_events, load_trades, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.epg.gate_variants import (
    AbsoluteThresholdGate,
    HawkesCumulativeGate,
    HawkesBuySideGate,
    BurstRatioGate,
)
from core.ofi.trade_ofi import compute_trade_ofi
from data.loaders.trades import _session_ns_bounds
from core.hawkes.engine import hawkes_replay_fixed_beta
from core.hawkes.forgetting import fit_hawkes_forgetting, fit_online, HawkesParams

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)
log = logging.getLogger(__name__)

# ── Constants ──────────────────────────────────────────────────────────
EPG_K = 5
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000
REFIT_INTERVAL = 50
REFIT_WINDOW = 10000
BETA_FIXED = 0.1

OUT_DIR = REPO_ROOT / "results" / "phase_epg_grt" / "sweep"
TRAIN_SAMPLE_PATH = REPO_ROOT / "results" / "phase_epg_grt" / "train_sample.json"


# ── Config grid construction ───────────────────────────────────────────

def build_configs() -> list[dict]:
    """Return the full list of 129 gate configs in order A → E."""
    configs = []

    # ── Variant A: ParticipationGate with p_open / p_close ──
    tau_vals = [120, 180, 240, 300]
    p_open_vals = [0.55, 0.60, 0.65]
    p_close_base = [0.30, 0.35, 0.40, 0.45, 0.50]
    for tau in tau_vals:
        for p_open in p_open_vals:
            # Fixed set + symmetric case
            p_closes = [pc for pc in p_close_base if pc <= p_open]
            p_closes.append(round(p_open, 2))  # symmetric (always included)
            seen = set()
            for pc in p_closes:
                pc_r = round(pc, 2)
                if pc_r not in seen:
                    seen.add(pc_r)
                    configs.append({
                        "variant": "a",
                        "config_id": f"var_a_t{tau}_po{int(round(p_open*100))}_pc{int(round(pc_r*100))}",
                        "tau": tau,
                        "p_open": p_open,
                        "p_close": pc_r,
                    })

    # ── Variant B: AbsoluteThresholdGate ──
    for k_abs in [1.5, 2.0, 3.0, 4.0, 5.0, 7.0, 10.0]:
        configs.append({
            "variant": "b",
            "config_id": f"var_b_k{k_abs}",
            "k_abs": k_abs,
            "half_life_seconds": 300.0,
        })

    # ── Variant C: HawkesCumulativeGate ──
    for beta_slow in [0.005, 0.01, 0.02]:
        for k_slow in [1.5, 2.0, 3.0, 4.0, 5.0]:
            configs.append({
                "variant": "c",
                "config_id": f"var_c_b{int(round(beta_slow * 1000)):03d}_k{k_slow}",
                "beta_slow": beta_slow,
                "k_slow": k_slow,
            })

    # ── Variant D: HawkesBuySideGate ──
    for beta_slow in [0.005, 0.01, 0.02]:
        for k_slow in [1.5, 2.0, 3.0, 4.0, 5.0]:
            configs.append({
                "variant": "d",
                "config_id": f"var_d_b{int(round(beta_slow * 1000)):03d}_k{k_slow}",
                "beta_slow": beta_slow,
                "k_slow": k_slow,
            })

    # ── Variant E: BurstRatioGate ──
    for window_n in [30, 60, 90, 120]:
        for threshold_r in [1.5, 2.0, 2.5, 3.0, 5.0]:
            configs.append({
                "variant": "e",
                "config_id": f"var_e_n{int(window_n)}_t{threshold_r}",
                "window_n": window_n,
                "threshold_r": threshold_r,
            })

    return configs


def _count_configs(configs: list[dict]) -> dict:
    counts: dict[str, int] = {}
    for c in configs:
        v = c["variant"]
        counts[v] = counts.get(v, 0) + 1
    return counts


# ── Hawkes replay (same as runner.py) ─────────────────────────────────

def _hawkes_replay_with_refit(
    t_sec, sides, rho, lambda_ref, init_params, rho_E,
    lam_buy_out, lam_sell_out, E_out, Edot_out, n_base_out,
):
    """Replay Hawkes with online MLE refitting. Identical to runner.py."""
    N = len(t_sec)
    if N == 0:
        return None

    cold_end = min(COLD_START_SIZE, N)
    if cold_end < 100:
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

    init_arr = np.array([
        init_params["alpha_buy_self"],
        init_params["alpha_sell_self"],
        init_params["mu_buy"], init_params["mu_sell"],
    ])

    params = fit_hawkes_forgetting(
        t_sec=t_sec[:cold_end], sides=sides[:cold_end],
        rho=rho, lambda_ref=lambda_ref,
        T=float(t_sec[cold_end - 1]), init_params=init_arr,
        n_restarts=5, beta_fixed=BETA_FIXED,
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

    R_buy = R_sell = 0.0
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
                lam_b = params.mu_buy
                lam_s = params.mu_sell
                lam_total = max(lam_b, 0.0) + max(lam_s, 0.0)
                E_val = lam_total / mu_total
                lam_buy_out[0] = max(lam_b, 0.0)
                lam_sell_out[0] = max(lam_s, 0.0)
                E_out[0] = E_val
                Edot_out[0] = 0.0
                R_buy = 1.0 if sides[0] == 1 else 0.0
                R_sell = 0.0 if sides[0] == 1 else 1.0
                E_prev = E_val
            else:
                dt = t_sec[i] - t_sec[i - 1]
                if dt > 0:
                    decay = np.exp(-params.beta * dt)
                    R_buy *= decay
                    R_sell *= decay
                lam_b = params.mu_buy + params.alpha_buy_self * R_buy
                lam_s = params.mu_sell + params.alpha_sell_self * R_sell
                lam_b = max(lam_b, 0.0)
                lam_s = max(lam_s, 0.0)
                lam_total = lam_b + lam_s
                E_val = lam_total / mu_total
                dt_capped = max(min(dt, 1.0), 1e-12)
                raw_slope = (E_val - E_prev) / dt_capped
                Edot_ema = rho_E * Edot_ema + (1.0 - rho_E) * raw_slope
                lam_buy_out[i] = lam_b
                lam_sell_out[i] = lam_s
                E_out[i] = E_val
                Edot_out[i] = Edot_ema
                R_buy += 1.0 if sides[i] == 1 else 0.0
                R_sell += 1.0 if sides[i] != 1 else 0.0
                E_prev = E_val

    return cold_start_params


# ── Pre-scan worker: estimate per-event λ_V_ref for Variant B ─────────

def _prescan_worker(args: dict) -> Optional[float]:
    """
    Quick estimate of pre-event λ_V_ref for Variant B global fallback.

    Uses fixed Hawkes params (no MLE) + simple arrival-rate signal to
    estimate T_event. Returns None if T_event not found or pre-event
    window < 60s.
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return None

        N = td.n_trades

        # Simple arrival-rate Hawkes proxy (no MLE, no sides)
        beta = fp["beta"]
        mu_total = fp["mu_buy"] + fp["mu_sell"]
        lambda_ref = mu_total
        k_mult = EPG_K

        lam_hat = np.zeros(N)
        R = 0.0
        alpha = fp["alpha_buy_self"] + fp["alpha_sell_self"]
        for i in range(N):
            if i > 0:
                dt = max(0.0, td.t_sec[i] - td.t_sec[i - 1])
                R *= math.exp(-beta * dt)
            lam_hat[i] = mu_total + alpha * R
            R += 1.0

        # EventAnchor to find T_event
        anchor = EventAnchor(lambda_ref=lambda_ref, k_multiplier=k_mult)
        t_event = None
        for i in range(N):
            t_ev = anchor.update(lam_hat[i], td.t_sec[i])
            if t_ev is not None:
                t_event = t_ev
                break

        if t_event is None:
            return None

        # Run AbsoluteThresholdGate pre-event accumulation
        gate_b = AbsoluteThresholdGate(
            k_abs=1.0,
            half_life_seconds=300.0,
            global_fallback_ref=0.0,
            warmup_seconds=EPG_WARMUP,
        )
        for i in range(N):
            dv = float(td.prices[i]) * float(td.sizes[i])
            gate_b.update(dv, td.t_sec[i], 0)
            if td.t_sec[i] >= t_event:
                break
        gate_b.activate(t_event)

        if gate_b.fallback_used or gate_b.lambda_v_ref <= 0:
            return None
        return gate_b.lambda_v_ref

    except Exception:
        return None


def compute_global_fallback_ref(
    events: list[dict],
    hawkes_params: dict,
    n_workers: int = 8,
) -> float:
    """Pre-scan all training events to compute the median pre-event λ_V_ref."""
    log.info(f"Pre-scan: computing global_fallback_ref from {len(events)} events...")
    t0 = time.time()

    work = [
        {"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
         "hawkes_params": hawkes_params}
        for e in events
    ]

    refs: list[float] = []
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_prescan_worker, w): w for w in work}
        for fut in as_completed(futures):
            val = fut.result()
            if val is not None and val > 0:
                refs.append(val)

    if not refs:
        fallback = 0.0
        log.warning("Pre-scan: no valid λ_V_ref computed; global_fallback_ref=0.0")
    else:
        fallback = float(np.median(refs))
        log.info(
            f"Pre-scan done in {time.time()-t0:.1f}s. "
            f"global_fallback_ref={fallback:.6f} "
            f"(from {len(refs)}/{len(events)} events)"
        )
    return fallback


# ── Per-gate replay on one event ───────────────────────────────────────

def _run_gate(
    cfg: dict,
    td,
    sides: np.ndarray,
    t_event: float,
    cold_start_params,
    global_fallback_ref: float,
    default_mu_buy: float,
    default_mu_sell: float,
) -> dict:
    """
    Replay one gate config on one pre-processed event. Fast: no Hawkes.

    Returns per-event metrics dict:
      n_trades, pnl_list, hold_list, pass_fraction,
      pass_windows, first_entry_delay
    """
    warmup = EPG_WARMUP
    variant = cfg["variant"]

    if variant == "a":
        gate = ParticipationGate(
            half_life_seconds=cfg["tau"],
            peak_threshold_p=cfg["p_open"],
            warmup_seconds=warmup,
            p_open=cfg["p_open"],
            p_close=cfg["p_close"],
        )
    elif variant == "b":
        gate = AbsoluteThresholdGate(
            k_abs=cfg["k_abs"],
            half_life_seconds=cfg.get("half_life_seconds", 300.0),
            global_fallback_ref=global_fallback_ref,
            warmup_seconds=warmup,
        )
    elif variant == "c":
        if cold_start_params is not None:
            mu_cum = cold_start_params.mu_buy + cold_start_params.mu_sell
        else:
            mu_cum = max(default_mu_buy + default_mu_sell, 1e-6)
        gate = HawkesCumulativeGate(
            beta_slow=cfg["beta_slow"],
            k_slow=cfg["k_slow"],
            mu_cum=max(mu_cum, 1e-6),
            warmup_seconds=warmup,
        )
    elif variant == "d":
        if cold_start_params is not None:
            mu_buy = cold_start_params.mu_buy
        else:
            mu_buy = default_mu_buy
        gate = HawkesBuySideGate(
            beta_slow=cfg["beta_slow"],
            k_slow=cfg["k_slow"],
            mu_buy=max(mu_buy, 1e-6),
            warmup_seconds=warmup,
        )
    elif variant == "e":
        gate = BurstRatioGate(
            window_n=cfg["window_n"],
            threshold_r=cfg["threshold_r"],
            warmup_seconds=warmup,
        )
    else:
        raise ValueError(f"Unknown variant: {variant}")

    gate.activate(t_event)

    N = td.n_trades
    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_sec = None
    entry_price = None

    pnl_list: list[float] = []
    hold_list: list[float] = []
    first_entry_delay: Optional[float] = None

    pass_windows: list[float] = []
    window_start: Optional[float] = None

    n_pass_ticks = 0
    n_postwarm_ticks = 0

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        t = td.t_sec[i]
        s = int(sides[i])

        state = gate.update(dv, t, s)

        # Pass fraction tracking
        if t >= t_event + warmup and state in (GateState.PASS, GateState.FAIL):
            n_postwarm_ticks += 1
            if state == GateState.PASS:
                n_pass_ticks += 1

        # PASS window duration tracking
        if state == GateState.PASS and prev_state != GateState.PASS:
            window_start = t
        elif state != GateState.PASS and prev_state == GateState.PASS:
            if window_start is not None:
                pass_windows.append(t - window_start)
            window_start = None

        # Entry / exit logic
        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                entry_t_sec = t
                entry_price = float(td.prices[min(i + 1, N - 1)])
                in_position = True
                if first_entry_delay is None:
                    first_entry_delay = t - t_event
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(td.prices[min(i + 1, N - 1)])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                hold = t - entry_t_sec
                pnl_list.append(pnl)
                hold_list.append(hold)
                in_position = False
                entry_t_sec = None
                entry_price = None

        prev_state = state

    # Session end: close open position
    if in_position:
        exit_price = float(td.prices[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        hold = td.t_sec[N - 1] - entry_t_sec
        pnl_list.append(pnl)
        hold_list.append(hold)

    if window_start is not None:
        pass_windows.append(td.t_sec[N - 1] - window_start)

    return {
        "n_trades": len(pnl_list),
        "pnl_list": pnl_list,
        "hold_list": hold_list,
        "pass_fraction": n_pass_ticks / n_postwarm_ticks if n_postwarm_ticks > 0 else 0.0,
        "pass_windows": pass_windows,
        "first_entry_delay": first_entry_delay,
    }


# ── Sweep worker: full processing for one event ────────────────────────

def _sweep_worker(args: dict) -> dict:
    """
    Full sweep worker: load event, run Hawkes, replay all 129 gate configs.

    Returns:
      status: 'ok' | 'skipped' | 'error'
      results: {config_id: per_event_metrics_dict}  (only when status='ok')
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    configs = args["configs"]
    global_fallback_ref = args["global_fallback_ref"]

    base = {"ticker": ticker, "date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades

        # Lee-Ready sides
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

        # Hawkes replay with online refit
        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (per_event_lref
                      if not math.isnan(per_event_lref) and per_event_lref > 0
                      else global_lref)

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # EventAnchor
        anchor_lref = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=anchor_lref, k_multiplier=EPG_K)
        if cold_start_params is not None:
            lref_epg = cold_start_params.mu_buy + cold_start_params.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)

        t_event = None
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None:
                t_event = t_ev
                break

        if t_event is None:
            return {**base, "status": "skipped", "reason": "no_t_event"}

        # Default mu values for gate construction
        default_mu_buy = fp["mu_buy"]
        default_mu_sell = fp["mu_sell"]

        # Run all 129 gate configs
        results: dict[str, dict] = {}
        for cfg in configs:
            r = _run_gate(
                cfg, td, sides, t_event, cold_start_params,
                global_fallback_ref, default_mu_buy, default_mu_sell,
            )
            results[cfg["config_id"]] = r

        return {**base, "status": "ok", "results": results}

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc()
        }


# ── Metric aggregation ─────────────────────────────────────────────────

def aggregate_config_metrics(event_results: list[dict]) -> dict:
    """Compute per-config summary metrics from per-event results list."""
    all_pnl: list[float] = []
    all_hold: list[float] = []
    all_pf: list[float] = []
    n_events_with_trades = 0

    per_event_pass_fracs: list[float] = []
    all_pass_windows: list[float] = []
    first_entry_delays: list[float] = []

    for r in event_results:
        per_event_pass_fracs.append(r["pass_fraction"])
        all_pass_windows.extend(r["pass_windows"])
        if r["first_entry_delay"] is not None:
            first_entry_delays.append(r["first_entry_delay"])
        if r["n_trades"] > 0:
            all_pnl.extend(r["pnl_list"])
            all_hold.extend(r["hold_list"])
            n_events_with_trades += 1

    n_trades = len(all_pnl)

    if n_trades == 0:
        pf = 0.0
        win_rate = 0.0
        mean_pnl = 0.0
        mean_hold = 0.0
        capture_rate = 0.0
    else:
        pnl_arr = np.array(all_pnl)
        hold_arr = np.array(all_hold)
        wins = pnl_arr > 0
        losses = pnl_arr < 0
        win_sum = float(np.sum(pnl_arr[wins])) if wins.any() else 0.0
        loss_sum = float(np.abs(np.sum(pnl_arr[losses]))) if losses.any() else 1e-10
        pf = win_sum / loss_sum
        win_rate = float(np.mean(wins))
        mean_pnl = float(np.mean(pnl_arr))
        mean_hold = float(np.mean(hold_arr))
        capture_rate = mean_pnl / mean_hold if mean_hold > 0 else 0.0

    pass_fraction = float(np.mean(per_event_pass_fracs)) if per_event_pass_fracs else 0.0
    mean_window_dur = float(np.mean(all_pass_windows)) if all_pass_windows else 0.0
    mean_first_delay = float(np.mean(first_entry_delays)) if first_entry_delays else None

    return {
        "n_trades": n_trades,
        "n_events_with_trades": n_events_with_trades,
        "profit_factor": round(pf, 4),
        "win_rate": round(win_rate * 100, 2),
        "mean_pnl_pct": round(mean_pnl, 4),
        "mean_hold_sec": round(mean_hold, 2),
        "pass_fraction": round(pass_fraction, 4),
        "mean_window_duration_sec": round(mean_window_dur, 2),
        "mean_first_entry_delay_sec": (
            round(mean_first_delay, 2) if mean_first_delay is not None else None
        ),
        "capture_rate": round(capture_rate, 6),
    }


# ── JSON utilities ─────────────────────────────────────────────────────

def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    raise TypeError(f"Not serializable: {type(obj)}")


def write_json_atomic(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False
    ) as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))
    log.info(f"Written: {path}")


# ══════════════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════════════

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--variants", type=str, default="abcde",
                        help="Which variants to run, e.g. 'a' or 'abcde'")
    args = parser.parse_args()

    # ── Load configs ──────────────────────────────────────────────────
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)
    with open(TRAIN_SAMPLE_PATH) as f:
        train_sample = json.load(f)

    val_start = boundary["val_split_start_date"]
    events = train_sample["events"]

    # Split check
    bad = [e for e in events if e["date"] >= val_start]
    if bad:
        log.error(f"HARD STOP: {len(bad)} events >= val_split_start_date. Aborting.")
        sys.exit(1)

    log.info(f"Training sample: {len(events)} events")

    # ── Build 129 configs ─────────────────────────────────────────────
    all_configs = build_configs()
    counts = _count_configs(all_configs)
    log.info(f"Config counts: {counts} → total {len(all_configs)}")
    assert len(all_configs) == 129, f"Expected 129 configs, got {len(all_configs)}"

    variants_to_run = set(args.variants.lower())
    configs_to_run = [c for c in all_configs if c["variant"] in variants_to_run]
    log.info(f"Running variants: {args.variants} → {len(configs_to_run)} configs")

    # ── Pre-scan: compute global_fallback_ref for Variant B ───────────
    if "b" in variants_to_run:
        global_fallback_ref = compute_global_fallback_ref(
            events, hawkes_params, n_workers=args.workers
        )
    else:
        global_fallback_ref = 0.0

    # ── Build sweep work items ────────────────────────────────────────
    rho = hawkes_params.get("rho", 0.99)

    work_items = [
        {
            "ticker": e["ticker"],
            "date": e["date"],
            "mom_pct": e["mom_pct"],
            "hawkes_params": hawkes_params,
            "rho": rho,
            "rho_E": rho,
            "q_bar_cfg": q_bar_cfg,
            "configs": configs_to_run,
            "global_fallback_ref": global_fallback_ref,
        }
        for e in events
    ]

    # ── Run parallel sweep ────────────────────────────────────────────
    log.info(
        f"Starting sweep: {len(work_items)} events × {len(configs_to_run)} configs "
        f"= {len(work_items) * len(configs_to_run):,} event-config pairs | "
        f"workers={args.workers}"
    )

    t0 = time.time()
    # per_config_results[config_id] = list of per-event result dicts
    per_config_results: dict[str, list[dict]] = {
        c["config_id"]: [] for c in configs_to_run
    }
    n_ok = n_skipped = n_errors = 0

    n_workers = min(args.workers, len(work_items)) if work_items else 1
    with ProcessPoolExecutor(max_workers=n_workers) as executor:
        futures = {executor.submit(_sweep_worker, item): item for item in work_items}
        for fut in as_completed(futures):
            r = fut.result()
            if r["status"] == "ok":
                n_ok += 1
                for config_id, ev_res in r["results"].items():
                    per_config_results[config_id].append(ev_res)
            elif r["status"] == "skipped":
                n_skipped += 1
            else:
                n_errors += 1
                log.warning(
                    f"Error: {r['ticker']} {r['date']}: {r.get('error', '')[:200]}"
                )

            done = n_ok + n_skipped + n_errors
            if done % 50 == 0 or done == len(work_items):
                log.info(
                    f"  Progress: {done}/{len(work_items)} events "
                    f"(ok={n_ok} skip={n_skipped} err={n_errors}) "
                    f"elapsed={time.time()-t0:.1f}s"
                )

    elapsed = time.time() - t0
    log.info(
        f"Sweep complete: {n_ok} events ok, {n_skipped} skipped, "
        f"{n_errors} errors in {elapsed:.1f}s"
    )

    # ── Aggregate per-config metrics ──────────────────────────────────
    log.info("Aggregating per-config metrics...")
    config_summaries: list[dict] = []
    for cfg in configs_to_run:
        cid = cfg["config_id"]
        ev_results = per_config_results[cid]
        metrics = aggregate_config_metrics(ev_results)
        summary = {
            "config_id": cid,
            "variant": cfg["variant"],
            **cfg,
            **metrics,
        }
        config_summaries.append(summary)

    # ── T3g escalation check ──────────────────────────────────────────
    n_qualified = sum(
        1 for s in config_summaries
        if s["pass_fraction"] >= 0.07 and s["n_trades"] >= 50
    )
    if n_qualified == 0:
        log.error(
            "HARD STOP (T3g): All configs disqualified "
            "(pass_fraction < 7% or n_trades < 50). Posting results and aborting."
        )
        # Write what we have before aborting
        _write_results(config_summaries, configs_to_run, all_configs)
        sys.exit(1)
    log.info(f"T3g escalation check: {n_qualified}/{len(config_summaries)} configs qualified.")

    # ── Write variant JSON files ──────────────────────────────────────
    _write_results(config_summaries, configs_to_run, all_configs)

    # ── Print summary ─────────────────────────────────────────────────
    _print_summary(config_summaries)


def _write_results(config_summaries: list[dict], configs_run: list[dict], all_configs: list[dict]) -> None:
    """Write variant JSON files and all_configs.json."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Group by variant
    by_variant: dict[str, list[dict]] = {}
    for s in config_summaries:
        v = s["variant"]
        by_variant.setdefault(v, []).append(s)

    variant_names = {"a": "variant_a", "b": "variant_b", "c": "variant_c",
                     "d": "variant_d", "e": "variant_e"}
    for v, summaries in by_variant.items():
        fname = variant_names.get(v, f"variant_{v}")
        write_json_atomic(
            {"meta": {"n_configs": len(summaries), "variant": v},
             "configs": summaries},
            OUT_DIR / f"{fname}.json",
        )

    # all_configs.json: all run configs sorted by n_trades desc (Borda done in T4)
    run_ids = {c["config_id"] for c in configs_run}
    all_written = [s for s in config_summaries]
    all_written.sort(key=lambda s: s.get("n_trades", 0), reverse=True)
    write_json_atomic(
        {"meta": {"n_configs": len(all_written), "n_events_input": None},
         "configs": all_written},
        OUT_DIR / "all_configs.json",
    )


def _print_summary(config_summaries: list[dict]) -> None:
    """Print human-readable summary table."""
    log.info("\n" + "=" * 70)
    log.info(f"{'config_id':<35} {'PF':>6} {'n_trades':>8} {'pass_frac':>9} {'delay_s':>8}")
    log.info("-" * 70)

    # Show top 20 by PF
    sorted_s = sorted(config_summaries, key=lambda x: x.get("profit_factor", 0), reverse=True)
    for s in sorted_s[:20]:
        disq = "DQ" if s["pass_fraction"] < 0.07 or s["n_trades"] < 50 else "  "
        delay = s.get("mean_first_entry_delay_sec")
        delay_str = f"{delay:.1f}" if delay is not None else "  N/A"
        log.info(
            f"{disq} {s['config_id']:<33} {s['profit_factor']:>6.4f} "
            f"{s['n_trades']:>8} {s['pass_fraction']:>9.3f} {delay_str:>8}"
        )
    log.info("=" * 70)


if __name__ == "__main__":
    main()
