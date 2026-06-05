"""
Shared worker functions for Phase WJI-POC.

Provides:
  quality_filter_worker(args)    — T2: Hawkes + SF trajectory per event
  wji_poc_worker(args)           — T3/T4: WJI gate + GRT baseline replay
  get_q_tilde_at_t_event(sf, t_event_ns) — Q_tilde at T_event bar
  aggregate_wji_metrics(event_results)   — metric aggregation including WJI diagnostics
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.epg.gate_variants import WJIGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import _hawkes_replay_with_refit, EPG_K, EPG_WARMUP
from tools.sweep_runner_opt2 import (
    precompute_sf_trajectory,
    sf_is_qualified_at,
    SFTrajectory,
    _run_gate_opt2_sf,
    aggregate_config_metrics_sf,
)

log = logging.getLogger(__name__)

LN2 = math.log(2)


# ══════════════════════════════════════════════════════════════════════
#  Helper: Q_tilde at T_event bar
# ══════════════════════════════════════════════════════════════════════

def get_q_tilde_at_t_event(
    sf: SFTrajectory,
    session_start_ns: int,
    t_event_sec: float,
) -> Optional[float]:
    """
    Return Q_tilde at the bar containing T_event, or None if out of range.

    Parameters
    ----------
    sf : SFTrajectory
    session_start_ns : int — absolute session start in nanoseconds
    t_event_sec : float — T_event as seconds from session start
    """
    if sf.n_bars == 0 or len(sf.bar_starts_ns) == 0:
        return None
    t_event_ns = session_start_ns + int(t_event_sec * NS_PER_SECOND)
    bar_pos = int(np.searchsorted(sf.bar_starts_ns, t_event_ns, side="right")) - 1
    if bar_pos < 0 or bar_pos >= sf.n_bars:
        return None
    return float(sf.q_tilde[bar_pos])


# ══════════════════════════════════════════════════════════════════════
#  T2 — Quality filter worker (Hawkes + SF per event)
# ══════════════════════════════════════════════════════════════════════

def quality_filter_worker(args: dict) -> dict:
    """
    Per-event worker for T2 quality filtering.

    Runs Hawkes to get T_event, computes SF trajectory, returns Q_tilde at T_event.

    Returns
    -------
    dict with keys: ticker, date, status, t_event (sec), q_tilde_at_t_event,
                    sf_n_bars, sf_mean_q_tilde, error
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    q_bar_cfg = args["q_bar_cfg"]

    base = {"ticker": ticker, "date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi_result = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides = ofi_result.sides

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lref
        )

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=fp, rho_E=rho,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

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

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)
        q_tilde_at_t_event = get_q_tilde_at_t_event(sf, start_ns, t_event)

        mu_buy = cold_start_params.mu_buy if cold_start_params is not None else fp["mu_buy"]

        return {
            **base,
            "status": "ok",
            "mom_pct": mom_pct,
            "t_event": t_event,
            "q_tilde_at_t_event": q_tilde_at_t_event,
            "sf_n_bars": sf.n_bars,
            "sf_mean_q_tilde": sf.mean_q_tilde,
            "mu_buy": float(mu_buy),
        }

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  WJI gate replay (T3/T4 core)
# ══════════════════════════════════════════════════════════════════════

def _compute_lambda_v_ref(td, t_event: float, tau_v: float) -> float:
    """
    Compute pre-event mean of lambda_V EMA over [session_start, T_event).
    Mirrors AbsoluteThresholdGate Variant-B reference computation.
    """
    decay_rate_v = LN2 / tau_v
    lambda_v = 0.0
    pre_event_lambdas: list[float] = []
    last_t: Optional[float] = None

    for i in range(td.n_trades):
        t = float(td.t_sec[i])
        dv = float(td.prices[i]) * float(td.sizes[i])
        if last_t is None:
            lambda_v = dv * decay_rate_v
        else:
            dt = max(0.0, t - last_t)
            lambda_v = lambda_v * math.exp(-decay_rate_v * dt) + dv * decay_rate_v
        last_t = t
        if t < t_event:
            pre_event_lambdas.append(lambda_v)
        else:
            break

    if pre_event_lambdas:
        return max(sum(pre_event_lambdas) / len(pre_event_lambdas), 1e-9)
    return 1e-9


def _run_wji_gate(
    cfg: dict,
    td,
    sides: np.ndarray,
    t_event: float,
    mu_buy: float,
    sf: Optional[SFTrajectory] = None,
) -> dict:
    """
    Replay WJIGate on one event with optional SF entry gate.

    Returns per-event metrics dict including WJI diagnostics:
      component_balance, pct_windows_with_prior_decay, n_pass_windows, lambda_v_ref
    """
    lambda_v_ref = _compute_lambda_v_ref(td, t_event, cfg["tau_v"])

    gate = WJIGate(
        alpha=cfg["alpha"],
        tau_v=cfg["tau_v"],
        beta_slow=cfg["beta_slow"],
        L_sec=cfg["L_sec"],
        tau_decay=cfg["tau_decay"],
        p_open=cfg["p_open"],
        p_close=cfg["p_close"],
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event, lambda_v_ref, max(float(mu_buy), 1e-9))

    use_sf = sf is not None and sf.n_bars > 0
    N = td.n_trades
    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_sec: Optional[float] = None
    entry_price: Optional[float] = None

    pnl_list: list[float] = []
    hold_list: list[float] = []
    pass_windows: list[float] = []
    window_start: Optional[float] = None
    first_entry_delay: Optional[float] = None
    n_pass_ticks = 0
    n_postwarm_ticks = 0
    n_entries_blocked_by_sf = 0
    entry_balance_list: list[float] = []

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        t = td.t_sec[i]

        state = gate.update(dv, t, int(sides[i]))

        if t >= t_event + EPG_WARMUP and state in (GateState.PASS, GateState.FAIL):
            n_postwarm_ticks += 1
            if state == GateState.PASS:
                n_pass_ticks += 1

        if state == GateState.PASS and prev_state != GateState.PASS:
            window_start = t
        elif state != GateState.PASS and prev_state == GateState.PASS:
            if window_start is not None:
                pass_windows.append(t - window_start)
            window_start = None

        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                sf_ok = True
                if use_sf:
                    sf_ok = sf_is_qualified_at(sf, int(td.timestamps[i]))
                    if not sf_ok:
                        n_entries_blocked_by_sf += 1

                if sf_ok:
                    entry_t_sec = t
                    entry_price = float(td.prices[min(i + 1, N - 1)])
                    in_position = True
                    if first_entry_delay is None:
                        first_entry_delay = t - t_event
                    nv = gate.norm_lambda_v
                    nb = gate.norm_lambda_buy
                    balance = (min(nv, nb) / max(nv, nb)) if max(nv, nb) > 1e-9 else 0.0
                    entry_balance_list.append(balance)
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(td.prices[min(i + 1, N - 1)])
                pnl_list.append((exit_price - entry_price) / entry_price * 100.0)
                hold_list.append(t - entry_t_sec)
                in_position = False
                entry_t_sec = None
                entry_price = None

        prev_state = state

    if in_position:
        exit_price = float(td.prices[N - 1])
        pnl_list.append((exit_price - entry_price) / entry_price * 100.0)
        hold_list.append(td.t_sec[N - 1] - entry_t_sec)

    if window_start is not None:
        pass_windows.append(td.t_sec[N - 1] - window_start)

    n_total_windows = len(gate.pass_windows)
    n_with_decay = sum(
        1 for w in gate.pass_windows
        if w["peak_at_prior_close"] > 0
        and w["peak_at_open"] < 0.90 * w["peak_at_prior_close"]
    )
    pct_prior_decay = n_with_decay / n_total_windows if n_total_windows > 0 else 0.0
    component_balance = (
        sum(entry_balance_list) / len(entry_balance_list)
        if entry_balance_list else 0.0
    )

    return {
        "n_trades": len(pnl_list),
        "pnl_list": pnl_list,
        "hold_list": hold_list,
        "pass_fraction": n_pass_ticks / n_postwarm_ticks if n_postwarm_ticks > 0 else 0.0,
        "pass_windows": pass_windows,
        "first_entry_delay": first_entry_delay,
        "n_entries_blocked_by_sf": n_entries_blocked_by_sf,
        "component_balance": component_balance,
        "pct_windows_with_prior_decay": pct_prior_decay,
        "n_pass_windows": n_total_windows,
        "lambda_v_ref": lambda_v_ref,
    }


# ══════════════════════════════════════════════════════════════════════
#  T3/T4 — Full event worker (WJI + GRT baseline)
# ══════════════════════════════════════════════════════════════════════

def wji_poc_worker(args: dict) -> dict:
    """
    Per-event worker for T3/T4: runs WJI POC config + GRT baseline on one event.

    args keys: ticker, date, mom_pct, hawkes_params, rho, q_bar_cfg,
               wji_cfg, baseline_cfg
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    q_bar_cfg = args["q_bar_cfg"]
    wji_cfg = args["wji_cfg"]
    baseline_cfg = args["baseline_cfg"]

    base = {"ticker": ticker, "date": date}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi_result = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides = ofi_result.sides

        lam_buy_out = np.zeros(N, dtype=np.float64)
        lam_sell_out = np.zeros(N, dtype=np.float64)
        E_out = np.zeros(N, dtype=np.float64)
        Edot_out = np.zeros(N, dtype=np.float64)
        n_base_out = np.zeros(N, dtype=np.float64)

        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lref
        )

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=fp, rho_E=rho,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

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

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)

        mu_buy = cold_start_params.mu_buy if cold_start_params is not None else fp["mu_buy"]

        # WJI gate replay
        wji_result = _run_wji_gate(wji_cfg, td, sides, t_event, mu_buy, sf=sf)

        # GRT baseline replay (ParticipationGate with SF)
        baseline_result = _run_gate_opt2_sf(
            baseline_cfg, td, sides, t_event, sf=sf,
            lambda_v_ref=max(mu_buy, 1e-9),
        )

        return {
            **base,
            "status": "ok",
            "t_event": t_event,
            "wji": wji_result,
            "baseline": baseline_result,
            "sf_n_bars": sf.n_bars,
            "sf_mean_q_tilde": sf.mean_q_tilde,
        }

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  Metric aggregation (WJI-aware)
# ══════════════════════════════════════════════════════════════════════

def aggregate_wji_metrics(event_results: list[dict]) -> dict:
    """
    Aggregate WJI-specific per-event metrics.

    Extends aggregate_config_metrics_sf with:
      component_balance         — mean balance across all entry ticks
      pct_windows_with_prior_decay — fraction of PASS windows where peak decayed
    """
    all_pnl: list[float] = []
    all_hold: list[float] = []
    all_balance: list[float] = []
    n_windows_total = 0
    n_windows_with_decay = 0
    per_event_pass_fracs: list[float] = []
    all_pass_windows: list[float] = []
    first_entry_delays: list[float] = []
    n_events_with_trades = 0
    n_blocked_total = 0

    for r in event_results:
        per_event_pass_fracs.append(r.get("pass_fraction", 0.0))
        all_pass_windows.extend(r.get("pass_windows", []))
        if r.get("first_entry_delay") is not None:
            first_entry_delays.append(r["first_entry_delay"])

        n_t = r.get("n_trades", 0)
        n_blocked_total += r.get("n_entries_blocked_by_sf", 0)

        if n_t > 0:
            all_pnl.extend(r.get("pnl_list", []))
            all_hold.extend(r.get("hold_list", []))
            n_events_with_trades += 1

        # WJI diagnostics (may be missing for baseline results)
        if "component_balance" in r and n_t > 0:
            all_balance.append(r["component_balance"])

        n_w = r.get("n_pass_windows", len(r.get("pass_windows", [])))
        n_windows_total += n_w
        pct_d = r.get("pct_windows_with_prior_decay", 0.0)
        n_windows_with_decay += round(pct_d * n_w)

    n_trades = len(all_pnl)
    if n_trades == 0:
        base_metrics = {
            "n_trades": 0, "n_events_with_trades": 0,
            "profit_factor": 0.0, "win_rate": 0.0,
            "mean_pnl_pct": 0.0, "total_pnl_pct": 0.0,
            "total_pnl_pct_per_event": 0.0,
            "mean_hold_sec": 0.0, "pass_fraction": 0.0,
            "mean_first_entry_delay_sec": 0.0,
            "n_entries_blocked_by_sf": n_blocked_total,
            "pct_entries_blocked": 0.0,
        }
    else:
        wins = [p for p in all_pnl if p > 0]
        losses = [abs(p) for p in all_pnl if p < 0]
        pf = round(sum(wins) / sum(losses), 4) if losses else float("inf")
        total_pnl = round(sum(all_pnl), 4)
        n_entries_total = n_trades + n_blocked_total
        pct_blocked = round(n_blocked_total / n_entries_total * 100.0, 2) if n_entries_total > 0 else 0.0

        base_metrics = {
            "n_trades": n_trades,
            "n_events_with_trades": n_events_with_trades,
            "profit_factor": pf,
            "win_rate": round(len(wins) / n_trades * 100, 2),
            "mean_pnl_pct": round(sum(all_pnl) / n_trades, 4),
            "total_pnl_pct": total_pnl,
            "total_pnl_pct_per_event": round(total_pnl / n_events_with_trades, 4) if n_events_with_trades > 0 else 0.0,
            "mean_hold_sec": round(sum(all_hold) / n_trades, 2),
            "pass_fraction": round(sum(per_event_pass_fracs) / len(per_event_pass_fracs), 4) if per_event_pass_fracs else 0.0,
            "mean_first_entry_delay_sec": round(sum(first_entry_delays) / len(first_entry_delays), 2) if first_entry_delays else 0.0,
            "n_entries_blocked_by_sf": n_blocked_total,
            "pct_entries_blocked": pct_blocked,
        }

    component_balance = round(sum(all_balance) / len(all_balance), 4) if all_balance else 0.0
    pct_windows_with_prior_decay = round(
        n_windows_with_decay / n_windows_total, 4
    ) if n_windows_total > 0 else 0.0

    return {
        **base_metrics,
        "component_balance": component_balance,
        "pct_windows_with_prior_decay": pct_windows_with_prior_decay,
        "n_pass_windows_total": n_windows_total,
    }
