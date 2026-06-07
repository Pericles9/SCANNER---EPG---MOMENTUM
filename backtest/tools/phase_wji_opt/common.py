"""
Shared worker functions for Phase WJI-OPT.

Signal computation + RunningMaxGate replay over cached Hawkes results.

Public API
----------
wji_opt_worker(args)     — per-event worker; returns per-trade records for all configs
build_config_grid()      — generate the 10-config p × hysteresis sweep
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

from data.loaders.trades import load_trades, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import NS_PER_SECOND
from core.epg.gate import GateState
from core.epg.gate_variants import RunningMaxGate
from core.ofi.trade_ofi import compute_trade_ofi
from tools.sweep_runner_opt2 import (
    precompute_sf_trajectory,
    sf_is_qualified_at,
    SFTrajectory,
)

log = logging.getLogger(__name__)

LN2 = math.log(2)
EPG_WARMUP = 300.0


# ══════════════════════════════════════════════════════════════════════
#  Config grid
# ══════════════════════════════════════════════════════════════════════

P_GRID = [0.55, 0.65, 0.70, 0.75, 0.80]
HYSTERESIS_GRID = ["single", "asym"]
P_CLOSE_ASYM = 0.30


def build_config_grid() -> list[dict]:
    """
    Generate the 10-config Stage 1 sweep grid:
      p ∈ {0.55, 0.65, 0.70, 0.75, 0.80} × hysteresis ∈ {single, asym}

    Returns list of config dicts with keys:
      config_id, signal, p, hysteresis, p_close, alpha
    """
    configs = []
    for hyst in HYSTERESIS_GRID:
        for p in P_GRID:
            p_close = P_CLOSE_ASYM if hyst == "asym" else p
            config_id = f"p{int(p*100):03d}_{hyst}"
            configs.append({
                "config_id": config_id,
                "p": p,
                "hysteresis": hyst,
                "p_close": p_close,
            })
    return configs


def build_alpha_grid(winning_p: float, winning_hyst: str) -> list[dict]:
    """
    Stage 2 alpha sweep grid (T5, conditional).
    Returns 4 configs at alpha ∈ {0.35, 0.50, 0.65, 0.80}.
    """
    p_close = P_CLOSE_ASYM if winning_hyst == "asym" else winning_p
    return [
        {
            "config_id": f"a{int(a*100):03d}_p{int(winning_p*100):03d}_{winning_hyst}",
            "p": winning_p,
            "hysteresis": winning_hyst,
            "p_close": p_close,
            "alpha": a,
        }
        for a in [0.35, 0.50, 0.65, 0.80]
    ]


# ══════════════════════════════════════════════════════════════════════
#  Signal computation
# ══════════════════════════════════════════════════════════════════════

def _compute_lambda_v_ref(prices, sizes, t_sec, t_event: float, tau_v: float) -> float:
    """Pre-event mean of λ_V EMA over [session_start, T_event)."""
    decay_rate = LN2 / tau_v
    lv = 0.0
    pre_lambdas: list[float] = []
    last_t: Optional[float] = None

    for i in range(len(t_sec)):
        t = float(t_sec[i])
        dv = float(prices[i]) * float(sizes[i])
        if last_t is None:
            lv = dv * decay_rate
        else:
            dt = max(0.0, t - last_t)
            lv = lv * math.exp(-decay_rate * dt) + dv * decay_rate
        last_t = t
        if t < t_event:
            pre_lambdas.append(lv)
        else:
            break

    return max(sum(pre_lambdas) / len(pre_lambdas), 1e-9) if pre_lambdas else 1e-9


def compute_wji_signal(
    prices,
    sizes,
    t_sec,
    sides,
    t_event: float,
    mu_buy: float,
    tau_v: float = 180.0,
    beta_slow: float = 0.01,
    alpha: float = 0.5,
) -> tuple[np.ndarray, float]:
    """
    Compute WJI signal array and λ_V_ref for one event.

    Returns (wji_array, lambda_v_ref) where wji_array has shape (N,) and
    wji_array[i] corresponds to t_sec[i].
    """
    N = len(t_sec)
    EPS = 1e-9
    decay_v = LN2 / tau_v
    lv_ref = _compute_lambda_v_ref(prices, sizes, t_sec, t_event, tau_v)
    mu_buy_safe = max(float(mu_buy), EPS)

    lv = 0.0
    lb = 0.0
    last_t: Optional[float] = None
    wji_arr = np.empty(N, dtype=np.float64)

    for i in range(N):
        t = float(t_sec[i])
        dv = float(prices[i]) * float(sizes[i])
        if last_t is None:
            lv = dv * decay_v
            lb = 0.0
        else:
            dt = max(0.0, t - last_t)
            lv = lv * math.exp(-decay_v * dt) + dv * decay_v
            lb *= math.exp(-beta_slow * dt)
        if int(sides[i]) == 1:
            lb += beta_slow
        last_t = t

        norm_v = lv / lv_ref
        norm_b = lb / mu_buy_safe
        wji_arr[i] = max(norm_v, EPS) ** alpha * max(norm_b, EPS) ** (1.0 - alpha)

    return wji_arr, lv_ref


def compute_lambda_v_signal(
    prices,
    sizes,
    t_sec,
    t_event: float,
    tau_v: float = 180.0,
) -> tuple[np.ndarray, float]:
    """
    Compute normalised λ_V signal array (norm_λ_V = λ_V / λ_V_ref).

    Returns (norm_lv_array, lambda_v_ref).
    """
    N = len(t_sec)
    EPS = 1e-9
    decay_v = LN2 / tau_v
    lv_ref = _compute_lambda_v_ref(prices, sizes, t_sec, t_event, tau_v)

    lv = 0.0
    last_t: Optional[float] = None
    sig_arr = np.empty(N, dtype=np.float64)

    for i in range(N):
        t = float(t_sec[i])
        dv = float(prices[i]) * float(sizes[i])
        if last_t is None:
            lv = dv * decay_v
        else:
            dt = max(0.0, t - last_t)
            lv = lv * math.exp(-decay_v * dt) + dv * decay_v
        last_t = t
        sig_arr[i] = lv / lv_ref

    return sig_arr, lv_ref


# ══════════════════════════════════════════════════════════════════════
#  Gate replay over pre-computed signal
# ══════════════════════════════════════════════════════════════════════

def _replay_gate(
    gate_cfg: dict,
    signal_arr: np.ndarray,
    t_sec,
    prices,
    t_event: float,
    sf: Optional[SFTrajectory],
    timestamps_ns,
) -> list[dict]:
    """
    Replay RunningMaxGate over a pre-computed signal array for one event.

    Returns a list of trade dicts, each with:
      pnl_pct, available_move_pct, hold_sec, year (empty — caller fills year),
      entry_t, exit_t
    """
    gate = RunningMaxGate(
        p=gate_cfg["p"],
        hysteresis=gate_cfg["hysteresis"],
        p_close=gate_cfg["p_close"],
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event)

    N = len(t_sec)
    use_sf = sf is not None and sf.n_bars > 0

    # Pre-compute reverse cumulative max for available_move_pct
    prices_arr = np.asarray(prices, dtype=np.float64)
    max_from = np.maximum.accumulate(prices_arr[::-1])[::-1]

    prev_state = GateState.INACTIVE
    in_position = False
    entry_t: Optional[float] = None
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None

    trades: list[dict] = []

    for i in range(N):
        t = float(t_sec[i])
        sig = float(signal_arr[i])

        state = gate.update(sig, t)

        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                sf_ok = True
                if use_sf:
                    sf_ok = sf_is_qualified_at(sf, int(timestamps_ns[i]))
                if sf_ok:
                    in_position = True
                    entry_t = t
                    entry_price = float(prices_arr[min(i + 1, N - 1)])
                    entry_idx = min(i + 1, N - 1)
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_price = float(prices_arr[min(i + 1, N - 1)])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
                trades.append({
                    "pnl_pct": pnl,
                    "available_move_pct": avail,
                    "hold_sec": t - entry_t,
                    "entry_t": entry_t,
                    "exit_t": t,
                    "year": "",
                })
                in_position = False
                entry_t = None
                entry_price = None
                entry_idx = None

        prev_state = state

    # Close any open position at last tick
    if in_position:
        exit_price = float(prices_arr[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
        trades.append({
            "pnl_pct": pnl,
            "available_move_pct": avail,
            "hold_sec": float(t_sec[N - 1]) - entry_t,
            "entry_t": entry_t,
            "exit_t": float(t_sec[N - 1]),
            "year": "",
        })

    return trades


# ══════════════════════════════════════════════════════════════════════
#  Per-event worker
# ══════════════════════════════════════════════════════════════════════

def wji_opt_worker(args: dict) -> dict:
    """
    Per-event worker for T3/T4 sweep.

    Uses cached t_event and mu_buy — does NOT re-run Hawkes.
    Runs all configs in args["configs"] over the same loaded data.

    args keys:
      ticker, date, mom_pct      — event identity
      t_event                    — from cache
      mu_buy                     — from cache
      q_bar_cfg                  — for OFI sides computation
      configs                    — list of config dicts (from build_config_grid)
      signal_type                — "wji" or "lambda_v"
      alpha                      — WJI alpha weight (ignored if signal_type="lambda_v")
      tau_v                      — dollar-volume EMA decay (seconds)
      beta_slow                  — buy-side kernel decay rate (WJI only)
      use_sf                     — bool, whether to apply SF entry gate
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    t_event = args["t_event"]
    mu_buy = args["mu_buy"]
    q_bar_cfg = args["q_bar_cfg"]
    configs = args["configs"]
    signal_type = args.get("signal_type", "wji")
    alpha = args.get("alpha", 0.5)
    tau_v = args.get("tau_v", 180.0)
    beta_slow = args.get("beta_slow", 0.01)
    use_sf = args.get("use_sf", True)
    year = date[:4]

    base = {"ticker": ticker, "date": date, "year": year}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        # Load quotes for OFI sides (needed for WJI buy-side channel + SF)
        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

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

        # SF trajectory for entry gate
        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns) if use_sf else None

        # Compute signal array (once, shared across all configs)
        if signal_type == "wji":
            signal_arr, lv_ref = compute_wji_signal(
                td.prices, td.sizes, td.t_sec, sides, t_event,
                mu_buy, tau_v=tau_v, beta_slow=beta_slow, alpha=alpha,
            )
        else:  # lambda_v
            signal_arr, lv_ref = compute_lambda_v_signal(
                td.prices, td.sizes, td.t_sec, t_event, tau_v=tau_v,
            )

        # Replay all configs
        config_results: dict[str, list[dict]] = {}
        for cfg in configs:
            trades = _replay_gate(
                cfg, signal_arr, td.t_sec, td.prices, t_event, sf, td.timestamps,
            )
            for tr in trades:
                tr["year"] = year
                tr["ticker"] = ticker
                tr["date"] = date
            config_results[cfg["config_id"]] = trades

        return {
            **base,
            "status": "ok",
            "t_event": t_event,
            "config_results": config_results,
            "lv_ref": lv_ref,
        }

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  Aggregation
# ══════════════════════════════════════════════════════════════════════

def aggregate_config_trades(
    event_results: list[dict],
    config_ids: list[str],
) -> dict[str, list[dict]]:
    """
    Collect all trade dicts per config across events.

    Returns dict: config_id → flat list of trade dicts.
    """
    all_trades: dict[str, list[dict]] = {cid: [] for cid in config_ids}
    for r in event_results:
        if r.get("status") != "ok":
            continue
        for cid, trades in r.get("config_results", {}).items():
            if cid in all_trades:
                all_trades[cid].extend(trades)
    return all_trades
