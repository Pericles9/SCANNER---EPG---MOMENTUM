"""
Shared worker functions for Phase WJI-SlowEMA.

Key differences from Phase WJI-OPT:
  - Halt detection is wired in: each event calls prepare_active_trades() and
    uses active_seconds (halt-adjusted elapsed time) for all EMA dt values.
  - Gate is WJISlowEMAGate (slow EMA reference) instead of RunningMaxGate.
  - Stagnation diagnostic: PASS/FAIL cycle count recorded per event.

T1e note
--------
The WJI-OPT baseline (PF=1.1881, 100-event val seed=42) was computed WITHOUT
halt-adjusted time (wall-clock dt throughout).  Results from this phase use
halt-adjusted dt and are therefore not directly comparable on a trade-count
basis.  PF comparisons are valid but interpret n_trades cautiously.

Public API
----------
build_config_grid()           — 25-config tau_slow × p_open sweep
wji_slow_ema_worker(args)     — per-event worker; returns per-trade records + cycle counts
aggregate_config_trades(...)  — flatten event results into per-config trade lists
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import NS_PER_SECOND
from core.epg.gate import GateState
from core.epg.gate_variants import WJISlowEMAGate
from core.features.luld_halt_detection import prepare_active_trades
from core.ofi.trade_ofi import compute_trade_ofi
from tools.sweep_runner_opt2 import (
    precompute_sf_trajectory,
    sf_is_qualified_at,
    SFTrajectory,
)

log = logging.getLogger(__name__)

LN2 = math.log(2)
EPG_WARMUP = 300.0

# Fixed signal params (inherited from WJI-OPT best config)
TAU_V = 180.0
BETA_SLOW = 0.01
ALPHA = 0.50

# Fixed p_close for all configs
P_CLOSE = 0.55


# ══════════════════════════════════════════════════════════════════════
#  Config grid  (T3 sweep)
# ══════════════════════════════════════════════════════════════════════

TAU_SLOW_GRID = [300, 600, 900, 1200, 1800]
P_OPEN_GRID = [0.70, 0.75, 0.80, 0.85, 0.90]


def build_config_grid() -> list[dict]:
    """
    25-config sweep: tau_slow ∈ {300, 600, 900, 1200, 1800} × p_open ∈ {0.70…0.90}.
    p_close = 0.55 is fixed across all configs.

    config_id format: t{tau_slow}_po{int(p_open*100)}
    """
    configs = []
    for tau in TAU_SLOW_GRID:
        for po in P_OPEN_GRID:
            config_id = f"t{tau}_po{int(po * 100)}"
            configs.append({
                "config_id": config_id,
                "tau_slow": float(tau),
                "p_open": po,
                "p_close": P_CLOSE,
            })
    return configs


# ══════════════════════════════════════════════════════════════════════
#  WJI signal computation with halt-adjusted dt
# ══════════════════════════════════════════════════════════════════════

def _compute_lambda_v_ref_halt(
    prices,
    sizes,
    active_seconds: np.ndarray,
    t_event_active_sec: float,
    tau_v: float,
) -> float:
    """
    Pre-event mean of λ_V EMA computed with halt-adjusted dt.

    Uses active_seconds[i] - active_seconds[i-1] as dt for each tick.
    Only accumulates ticks with active_seconds[i] < t_event_active_sec.
    """
    decay_rate = LN2 / tau_v
    lv = 0.0
    pre_lambdas: list[float] = []

    for i in range(len(active_seconds)):
        t_act = float(active_seconds[i])
        dv = float(prices[i]) * float(sizes[i])
        if i == 0:
            lv = dv * decay_rate
        else:
            dt = max(0.0, float(active_seconds[i]) - float(active_seconds[i - 1]))
            lv = lv * math.exp(-decay_rate * dt) + dv * decay_rate

        if t_act < t_event_active_sec:
            pre_lambdas.append(lv)
        else:
            break

    return max(sum(pre_lambdas) / len(pre_lambdas), 1e-9) if pre_lambdas else 1e-9


def compute_wji_signal_halt_adjusted(
    prices,
    sizes,
    sides,
    active_seconds: np.ndarray,
    t_event_active_sec: float,
    mu_buy: float,
    tau_v: float = TAU_V,
    beta_slow: float = BETA_SLOW,
    alpha: float = ALPHA,
) -> tuple[np.ndarray, float]:
    """
    Compute WJI signal array using halt-adjusted dt from active_seconds.

    Parameters
    ----------
    prices, sizes, sides : arrays aligned with active_seconds (trimmed set)
    active_seconds       : halt-adjusted elapsed time from first trade (N,)
    t_event_active_sec   : active-seconds timestamp of T_event
    mu_buy               : buy-arrival baseline rate (from Hawkes cache)
    tau_v, beta_slow, alpha : signal hyperparameters

    Returns
    -------
    (wji_array, lambda_v_ref)
    """
    N = len(active_seconds)
    EPS = 1e-9
    decay_v = LN2 / tau_v
    lv_ref = _compute_lambda_v_ref_halt(prices, sizes, active_seconds, t_event_active_sec, tau_v)
    mu_buy_safe = max(float(mu_buy), EPS)

    lv = 0.0
    lb = 0.0
    wji_arr = np.empty(N, dtype=np.float64)

    for i in range(N):
        dv = float(prices[i]) * float(sizes[i])
        if i == 0:
            lv = dv * decay_v
            lb = 0.0
        else:
            dt = max(0.0, float(active_seconds[i]) - float(active_seconds[i - 1]))
            lv = lv * math.exp(-decay_v * dt) + dv * decay_v
            lb *= math.exp(-beta_slow * dt)
        if int(sides[i]) == 1:
            lb += beta_slow

        norm_v = lv / lv_ref
        norm_b = lb / mu_buy_safe
        wji_arr[i] = max(norm_v, EPS) ** alpha * max(norm_b, EPS) ** (1.0 - alpha)

    return wji_arr, lv_ref


# ══════════════════════════════════════════════════════════════════════
#  Gate replay
# ══════════════════════════════════════════════════════════════════════

def _replay_wji_slow_ema_gate(
    gate_cfg: dict,
    wji_arr: np.ndarray,
    active_seconds: np.ndarray,
    prices,
    t_event_active_sec: float,
    sf: Optional[SFTrajectory],
    timestamps_ns,
) -> tuple[list[dict], int]:
    """
    Replay WJISlowEMAGate over a pre-computed WJI signal array.

    Returns (trades, cycle_count) where:
      trades      — list of trade dicts (pnl_pct, available_move_pct, hold_sec, …)
      cycle_count — number of PASS↔FAIL transitions (stagnation diagnostic)
    """
    gate = WJISlowEMAGate(
        tau_slow=gate_cfg["tau_slow"],
        p_open=gate_cfg["p_open"],
        p_close=gate_cfg["p_close"],
        warmup_seconds=EPG_WARMUP,
    )
    gate.activate(t_event_active_sec)

    N = len(active_seconds)
    use_sf = sf is not None and sf.n_bars > 0
    prices_arr = np.asarray(prices, dtype=np.float64)
    max_from = np.maximum.accumulate(prices_arr[::-1])[::-1]

    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_active: Optional[float] = None
    entry_price: Optional[float] = None
    entry_idx: Optional[int] = None

    trades: list[dict] = []
    cycle_count = 0

    for i in range(N):
        act = float(active_seconds[i])
        dt_active = act - float(active_seconds[i - 1]) if i > 0 else 0.0
        wji = float(wji_arr[i])

        state = gate.update(wji, dt_active)

        # Count PASS↔FAIL transitions (stagnation diagnostic)
        if prev_state == GateState.PASS and state == GateState.FAIL:
            cycle_count += 1
        elif prev_state == GateState.FAIL and state == GateState.PASS:
            cycle_count += 1

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
                    entry_t_active = act
                    entry_idx = min(i + 1, N - 1)
                    entry_price = float(prices_arr[entry_idx])
        else:
            if prev_state == GateState.PASS and state != GateState.PASS:
                exit_idx = min(i + 1, N - 1)
                exit_price = float(prices_arr[exit_idx])
                pnl = (exit_price - entry_price) / entry_price * 100.0
                avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
                trades.append({
                    "pnl_pct": pnl,
                    "available_move_pct": avail,
                    "hold_sec": act - entry_t_active,
                    "entry_t": entry_t_active,
                    "exit_t": act,
                    "year": "",
                })
                in_position = False
                entry_t_active = None
                entry_price = None
                entry_idx = None

        prev_state = state

    # Close any open position at final tick
    if in_position:
        exit_price = float(prices_arr[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        avail = max(float(max_from[entry_idx]) / entry_price - 1.0, 0.0) * 100.0
        trades.append({
            "pnl_pct": pnl,
            "available_move_pct": avail,
            "hold_sec": float(active_seconds[N - 1]) - entry_t_active,
            "entry_t": entry_t_active,
            "exit_t": float(active_seconds[N - 1]),
            "year": "",
        })

    return trades, cycle_count


# ══════════════════════════════════════════════════════════════════════
#  Per-event worker
# ══════════════════════════════════════════════════════════════════════

def wji_slow_ema_worker(args: dict) -> dict:
    """
    Per-event worker for Phase WJI-SlowEMA T3 sweep.

    Wires in halt detection (T1b): calls prepare_active_trades() per event
    and uses active_seconds as the dt source for all EMA updates.
    Does NOT re-run Hawkes — uses cached t_event and mu_buy.

    args keys
    ---------
    ticker, date, mom_pct    — event identity
    t_event                  — seconds-from-first-trade (wall-clock, from cache)
    mu_buy                   — buy-arrival baseline (from cache)
    q_bar_cfg                — for OFI sides computation
    configs                  — list of 25 gate config dicts
    tau_v, beta_slow, alpha  — WJI signal hyperparameters
    use_sf                   — bool, whether to apply SF entry gate
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    t_event_wall = args["t_event"]      # wall-clock seconds from first trade
    mu_buy = args["mu_buy"]
    q_bar_cfg = args["q_bar_cfg"]
    configs = args["configs"]
    tau_v = args.get("tau_v", TAU_V)
    beta_slow = args.get("beta_slow", BETA_SLOW)
    alpha = args.get("alpha", ALPHA)
    use_sf = args.get("use_sf", True)
    year = date[:4]

    base = {"ticker": ticker, "date": date, "year": year}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}

        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        # ── OFI sides (on full trades, before halt trimming) ────────────
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
        sides_full = ofi_result.sides

        # ── Halt detection (T1b) ─────────────────────────────────────────
        df_trades = pd.DataFrame(
            {
                "price": td.prices,
                "size": td.sizes.astype(float),
                "_orig_idx": np.arange(len(td.prices)),
            },
            index=pd.to_datetime(td.timestamps, unit="ns").tz_localize(None),
        )
        trimmed_df, active_seconds, _halt_meta = prepare_active_trades(
            df_trades,
            price_col="price",
            size_col="size",
            include_extended=True,
            halt_gap_seconds=300,
        )
        if len(trimmed_df) < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades_post_halt"}

        orig_idx = trimmed_df["_orig_idx"].values.astype(int)
        trimmed_prices = td.prices[orig_idx]
        trimmed_sizes = td.sizes[orig_idx]
        trimmed_ts_ns = td.timestamps[orig_idx]
        trimmed_sides = sides_full[orig_idx]

        # ── Map T_event to active-seconds coordinates ────────────────────
        t_event_ns = td.timestamps[0] + int(t_event_wall * NS_PER_SECOND)
        t_event_pd = pd.Timestamp(int(t_event_ns), unit="ns").tz_localize(None)
        idx_event = int(np.searchsorted(trimmed_df.index, t_event_pd, side="left"))
        if idx_event >= len(active_seconds):
            idx_event = len(active_seconds) - 1
        t_event_active_sec = float(active_seconds[idx_event])

        # ── SF trajectory (for entry gate) ───────────────────────────────
        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns) if use_sf else None

        # ── WJI signal with halt-adjusted dt ────────────────────────────
        wji_arr, lv_ref = compute_wji_signal_halt_adjusted(
            trimmed_prices, trimmed_sizes, trimmed_sides, active_seconds,
            t_event_active_sec, mu_buy,
            tau_v=tau_v, beta_slow=beta_slow, alpha=alpha,
        )

        # ── Replay all 25 gate configs ───────────────────────────────────
        config_results: dict[str, dict] = {}
        for cfg in configs:
            trades, cycle_count = _replay_wji_slow_ema_gate(
                cfg, wji_arr, active_seconds, trimmed_prices,
                t_event_active_sec, sf, trimmed_ts_ns,
            )
            for tr in trades:
                tr["year"] = year
                tr["ticker"] = ticker
                tr["date"] = date
            config_results[cfg["config_id"]] = {
                "trades": trades,
                "cycle_count": cycle_count,
            }

        return {
            **base,
            "status": "ok",
            "t_event_active_sec": t_event_active_sec,
            "n_halts": len(_halt_meta.get("halts", [])),
            "config_results": config_results,
            "lv_ref": float(lv_ref),
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
) -> dict[str, dict]:
    """
    Collect trade dicts and cycle counts per config across all events.

    Returns dict: config_id → {"trades": [...], "cycle_counts": [...]}
    """
    agg: dict[str, dict] = {
        cid: {"trades": [], "cycle_counts": []}
        for cid in config_ids
    }
    for r in event_results:
        if r.get("status") != "ok":
            continue
        for cid, cr in r.get("config_results", {}).items():
            if cid in agg:
                agg[cid]["trades"].extend(cr.get("trades", []))
                agg[cid]["cycle_counts"].append(cr.get("cycle_count", 0))
    return agg
