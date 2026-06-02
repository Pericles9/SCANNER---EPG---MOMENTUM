"""
Shared sweep infrastructure for Phase EPG-OPT2.

Provides:
  build_stage1_configs()          — 84 Stage 1 level-gate configs (no cooling)
  build_stage2_configs(base_ids)  — cooling sweep on selected base configs
  _run_gate_opt2(...)             — gate replay with max_price_during_hold tracking
  _sweep_worker_opt2(args)        — multiprocessing worker (event → all configs)
  aggregate_config_metrics_opt2() — new metrics: capture_fraction, capture_rate
  dq_and_rank(rows)               — DQ + Borda ranking

All Stage 1 configs use variant 'a' (ParticipationGate) with m_cool_sec=0 (no cooling).
Stage 2 configs extend Stage 1 with m_cool_sec > 0 and tau_cool_sec.
"""
from __future__ import annotations

import logging
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.ofi.trade_ofi import compute_trade_ofi
from tools.t3_sweep_runner import (
    _hawkes_replay_with_refit,
    compute_global_fallback_ref,
    EPG_K,
    EPG_WARMUP,
)

log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
#  Config grid construction
# ══════════════════════════════════════════════════════════════════════

def build_stage1_configs() -> list[dict]:
    """
    84 Stage 1 configs: level gate, no cooling.

    Grid:
      τ ∈ {120, 180, 240, 300}
      p_open ∈ {0.55, 0.60, 0.65}
      p_close ∈ {0.15, 0.20, 0.25, 0.30, 0.35, 0.40} ∪ {p_open}
      constraint: p_close ≤ p_open

    ID: s1_t{tau}_po{int(p_open*100)}_pc{int(p_close*100)}
    """
    taus = [120, 180, 240, 300]
    p_opens = [0.55, 0.60, 0.65]
    p_close_grid = [0.15, 0.20, 0.25, 0.30, 0.35, 0.40]

    configs = []
    for tau in taus:
        for p_open in p_opens:
            seen_pcs = set()
            for p_close in p_close_grid:
                if p_close <= p_open:
                    config_id = (
                        f"s1_t{int(tau)}_po{int(round(p_open * 100))}"
                        f"_pc{int(round(p_close * 100))}"
                    )
                    configs.append({
                        "config_id": config_id,
                        "variant": "a",
                        "tau": float(tau),
                        "p_open": p_open,
                        "p_close": p_close,
                        "m_cool_sec": 0.0,
                        "tau_cool_sec": 120.0,
                    })
                    seen_pcs.add(round(p_close, 4))
            # Always include symmetric (p_close == p_open)
            if round(p_open, 4) not in seen_pcs:
                config_id = (
                    f"s1_t{int(tau)}_po{int(round(p_open * 100))}"
                    f"_pc{int(round(p_open * 100))}"
                )
                configs.append({
                    "config_id": config_id,
                    "variant": "a",
                    "tau": float(tau),
                    "p_open": p_open,
                    "p_close": p_open,
                    "m_cool_sec": 0.0,
                    "tau_cool_sec": 120.0,
                })

    return configs


def build_stage2_configs(
    base_config_ids: list[str],
    all_stage1: list[dict],
) -> list[dict]:
    """
    Stage 2 cooling sweep on top 15 Stage 1 configs + s1_t120_po65_pc65.

    Standard grid (all base configs):
      M_cool_sec ∈ {30, 60, 120, 180}
      τ_cool_sec ∈ {60, 120, 240}  → 12 combos per base

    Fine grid (s1_t120_po65_pc65 only):
      M_cool_sec ∈ {15, 45, 90}
      τ_cool_sec ∈ {45, 90, 180}  → 9 additional combos

    ID: s2_t{tau}_po{po}_pc{pc}_mc{M_cool}_tc{tau_cool}
    """
    base_by_id = {c["config_id"]: c for c in all_stage1}

    standard_m = [30, 60, 120, 180]
    standard_tc = [60, 120, 240]
    fine_m = [15, 45, 90]
    fine_tc = [45, 90, 180]
    t120_id = "s1_t120_po65_pc65"

    configs = []
    for base_id in base_config_ids:
        base = base_by_id.get(base_id)
        if base is None:
            log.warning("Stage 2: base config %s not found in Stage 1", base_id)
            continue

        tau = int(base["tau"])
        po = int(round(base["p_open"] * 100))
        pc = int(round(base["p_close"] * 100))
        tag = f"t{tau}_po{po}_pc{pc}"

        # Standard grid
        for m in standard_m:
            for tc in standard_tc:
                config_id = f"s2_{tag}_mc{m}_tc{tc}"
                configs.append({
                    "config_id": config_id,
                    "base_config_id": base_id,
                    "variant": "a",
                    "tau": base["tau"],
                    "p_open": base["p_open"],
                    "p_close": base["p_close"],
                    "m_cool_sec": float(m),
                    "tau_cool_sec": float(tc),
                })

        # Fine grid for t120 only
        if base_id == t120_id:
            for m in fine_m:
                for tc in fine_tc:
                    config_id = f"s2_{tag}_mc{m}_tc{tc}"
                    if not any(c["config_id"] == config_id for c in configs):
                        configs.append({
                            "config_id": config_id,
                            "base_config_id": base_id,
                            "variant": "a",
                            "tau": base["tau"],
                            "p_open": base["p_open"],
                            "p_close": base["p_close"],
                            "m_cool_sec": float(m),
                            "tau_cool_sec": float(tc),
                        })

    return configs


# ══════════════════════════════════════════════════════════════════════
#  Per-gate replay (variant A with cooling, tracks max_price_during_hold)
# ══════════════════════════════════════════════════════════════════════

def _run_gate_opt2(
    cfg: dict,
    td,
    sides: np.ndarray,
    t_event: float,
) -> dict:
    """
    Replay one variant-A gate config on one pre-processed event.

    Returns per-event metrics including per-trade max_price_during_hold and
    entry_price (needed for capture_fraction calculation).
    """
    gate = ParticipationGate(
        half_life_seconds=cfg["tau"],
        peak_threshold_p=cfg["p_open"],
        warmup_seconds=EPG_WARMUP,
        p_open=cfg["p_open"],
        p_close=cfg["p_close"],
        m_cool_sec=cfg.get("m_cool_sec", 0.0),
        tau_cool_sec=cfg.get("tau_cool_sec", 120.0),
    )
    gate.activate(t_event)

    N = td.n_trades
    prev_state = GateState.INACTIVE
    in_position = False
    entry_t_sec: Optional[float] = None
    entry_price: Optional[float] = None
    position_max_price: Optional[float] = None

    pnl_list: list[float] = []
    hold_list: list[float] = []
    max_price_list: list[float] = []
    entry_price_list: list[float] = []
    pass_windows: list[float] = []
    window_start: Optional[float] = None
    first_entry_delay: Optional[float] = None
    n_pass_ticks = 0
    n_postwarm_ticks = 0

    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        t = td.t_sec[i]

        state = gate.update(dv, t)

        # Pass fraction
        if t >= t_event + EPG_WARMUP and state in (GateState.PASS, GateState.FAIL):
            n_postwarm_ticks += 1
            if state == GateState.PASS:
                n_pass_ticks += 1

        # PASS window durations
        if state == GateState.PASS and prev_state != GateState.PASS:
            window_start = t
        elif state != GateState.PASS and prev_state == GateState.PASS:
            if window_start is not None:
                pass_windows.append(t - window_start)
            window_start = None

        # Running max price while in position
        if in_position:
            cur_p = float(td.prices[i])
            if position_max_price is None or cur_p > position_max_price:
                position_max_price = cur_p

        # Entry / exit (EPG window close only)
        if not in_position:
            rising_edge = (
                state == GateState.PASS
                and prev_state in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL)
            )
            if rising_edge:
                entry_t_sec = t
                entry_price = float(td.prices[min(i + 1, N - 1)])
                position_max_price = entry_price
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
                max_price_list.append(
                    position_max_price if position_max_price is not None else entry_price
                )
                entry_price_list.append(entry_price)
                in_position = False
                entry_t_sec = None
                entry_price = None
                position_max_price = None

        prev_state = state

    # Session end — close any open position
    if in_position:
        exit_price = float(td.prices[N - 1])
        pnl = (exit_price - entry_price) / entry_price * 100.0
        hold = td.t_sec[N - 1] - entry_t_sec
        pnl_list.append(pnl)
        hold_list.append(hold)
        max_price_list.append(
            position_max_price if position_max_price is not None else exit_price
        )
        entry_price_list.append(entry_price)

    if window_start is not None:
        pass_windows.append(td.t_sec[N - 1] - window_start)

    return {
        "n_trades": len(pnl_list),
        "pnl_list": pnl_list,
        "hold_list": hold_list,
        "max_price_list": max_price_list,
        "entry_price_list": entry_price_list,
        "pass_fraction": n_pass_ticks / n_postwarm_ticks if n_postwarm_ticks > 0 else 0.0,
        "pass_windows": pass_windows,
        "first_entry_delay": first_entry_delay,
    }


# ══════════════════════════════════════════════════════════════════════
#  Multiprocessing sweep worker
# ══════════════════════════════════════════════════════════════════════

def _sweep_worker_opt2(args: dict) -> dict:
    """
    Full sweep worker for EPG-OPT2: load event, run Hawkes, replay all configs.

    args keys: ticker, date, mom_pct, hawkes_params, rho, rho_E,
               q_bar_cfg, configs, (global_fallback_ref unused for variant A)
    """
    ticker = args["ticker"]
    date = args["date"]
    mom_pct = args["mom_pct"]
    fp = args["hawkes_params"]
    rho = args["rho"]
    rho_E = args["rho_E"]
    q_bar_cfg = args["q_bar_cfg"]
    configs = args["configs"]

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
        lambda_ref = (
            per_event_lref
            if not math.isnan(per_event_lref) and per_event_lref > 0
            else global_lref
        )

        cold_start_params = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides,
            rho=rho, lambda_ref=lambda_ref, init_params=fp, rho_E=rho_E,
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out

        # EventAnchor — T_event detection
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

        # Run all configs
        results: dict[str, dict] = {}
        for cfg in configs:
            results[cfg["config_id"]] = _run_gate_opt2(cfg, td, sides, t_event)

        return {**base, "status": "ok", "results": results}

    except Exception as e:
        import traceback
        return {
            **base, "status": "error",
            "error": str(e), "traceback": traceback.format_exc(),
        }


# ══════════════════════════════════════════════════════════════════════
#  Metric aggregation
# ══════════════════════════════════════════════════════════════════════

def aggregate_config_metrics_opt2(event_results: list[dict]) -> dict:
    """
    Compute per-config summary metrics from per-event results list.

    New metrics vs EPG-GRT:
      capture_fraction = mean(pnl_pct / available_move_pct)
        where available_move_pct = (max_price - entry_price) / entry_price * 100
        For trades where available_move_pct ≤ 0, contribution = 0.
      capture_rate = mean_pnl_pct / mean_hold_sec
    """
    all_pnl: list[float] = []
    all_hold: list[float] = []
    all_cf: list[float] = []
    per_event_pass_fracs: list[float] = []
    all_pass_windows: list[float] = []
    first_entry_delays: list[float] = []
    n_events_with_trades = 0

    for r in event_results:
        per_event_pass_fracs.append(r["pass_fraction"])
        all_pass_windows.extend(r["pass_windows"])
        if r["first_entry_delay"] is not None:
            first_entry_delays.append(r["first_entry_delay"])
        if r["n_trades"] > 0:
            all_pnl.extend(r["pnl_list"])
            all_hold.extend(r["hold_list"])
            n_events_with_trades += 1
            for pnl, ep, mp in zip(r["pnl_list"], r["entry_price_list"], r["max_price_list"]):
                if ep > 0:
                    avail = (mp - ep) / ep * 100.0
                else:
                    avail = 0.0
                all_cf.append(pnl / avail if avail > 0 else 0.0)

    n_trades = len(all_pnl)
    if n_trades == 0:
        return {
            "n_trades": 0, "n_events_with_trades": 0,
            "profit_factor": 0.0, "win_rate": 0.0,
            "mean_pnl_pct": 0.0, "mean_hold_sec": 0.0,
            "pass_fraction": 0.0, "mean_window_duration_sec": 0.0,
            "mean_first_entry_delay_sec": 0.0,
            "capture_fraction": 0.0, "capture_rate": 0.0,
        }

    wins = [p for p in all_pnl if p > 0]
    losses = [abs(p) for p in all_pnl if p < 0]
    pf = round(sum(wins) / sum(losses), 4) if losses else float("inf")
    wr = round(len(wins) / n_trades * 100, 2)
    mean_pnl = round(sum(all_pnl) / n_trades, 4)
    mean_hold = round(sum(all_hold) / n_trades, 2)
    pass_frac = round(sum(per_event_pass_fracs) / len(per_event_pass_fracs), 4)
    mean_window = round(
        sum(all_pass_windows) / len(all_pass_windows) if all_pass_windows else 0.0, 2
    )
    mean_delay = round(
        sum(first_entry_delays) / len(first_entry_delays) if first_entry_delays else 0.0, 2
    )
    mean_cf = round(sum(all_cf) / len(all_cf) if all_cf else 0.0, 4)
    capture_rate = round(mean_pnl / mean_hold if mean_hold > 0 else 0.0, 6)

    return {
        "n_trades": n_trades,
        "n_events_with_trades": n_events_with_trades,
        "profit_factor": pf,
        "win_rate": wr,
        "mean_pnl_pct": mean_pnl,
        "mean_hold_sec": mean_hold,
        "pass_fraction": pass_frac,
        "mean_window_duration_sec": mean_window,
        "mean_first_entry_delay_sec": mean_delay,
        "capture_fraction": mean_cf,
        "capture_rate": capture_rate,
    }


# ══════════════════════════════════════════════════════════════════════
#  DQ + Borda ranking
# ══════════════════════════════════════════════════════════════════════

DQ_PF_MIN = 1.20
DQ_PASS_FRAC_MIN = 0.07
DQ_N_TRADES_MIN = 50


def dq_and_rank(rows: list[dict]) -> list[dict]:
    """
    Apply DQ criteria and compute Borda ranking.

    DQ criteria (any one triggers):
      - PF < 1.20
      - pass_fraction < 7%
      - n_trades < 50

    Borda = rank_capture_fraction + rank_capture_rate  (lower = better, 1-indexed)
    Ranks assigned within non-DQ configs only.
    DQ'd configs get borda_score = None.

    Tiebreak within same borda_score: higher tau, then lower (p_open - p_close).
    """
    for row in rows:
        pf = row.get("profit_factor", 0.0)
        pf = 0.0 if not math.isfinite(pf) else pf
        dq = False
        dq_reasons = []
        if pf < DQ_PF_MIN:
            dq = True
            dq_reasons.append(f"pf={pf:.4f}<{DQ_PF_MIN}")
        if row.get("pass_fraction", 0.0) < DQ_PASS_FRAC_MIN:
            dq = True
            dq_reasons.append(f"pass_frac={row['pass_fraction']:.4f}<{DQ_PASS_FRAC_MIN}")
        if row.get("n_trades", 0) < DQ_N_TRADES_MIN:
            dq = True
            dq_reasons.append(f"n_trades={row['n_trades']}<{DQ_N_TRADES_MIN}")
        row["disqualified"] = dq
        row["dq_reason"] = dq_reasons

    non_dq = [r for r in rows if not r["disqualified"]]

    def _rank(items: list[dict], key: str, higher_is_better: bool) -> dict:
        sorted_items = sorted(
            items,
            key=lambda x: x.get(key, 0.0),
            reverse=higher_is_better,
        )
        ranks = {}
        for rank, item in enumerate(sorted_items, start=1):
            ranks[item["config_id"]] = rank
        return ranks

    rank_cf = _rank(non_dq, "capture_fraction", higher_is_better=True)
    rank_cr = _rank(non_dq, "capture_rate", higher_is_better=True)

    for row in rows:
        cid = row["config_id"]
        if row["disqualified"]:
            row["rank_capture_fraction"] = None
            row["rank_capture_rate"] = None
            row["borda_score"] = None
        else:
            rcf = rank_cf[cid]
            rcr = rank_cr[cid]
            row["rank_capture_fraction"] = rcf
            row["rank_capture_rate"] = rcr
            row["borda_score"] = rcf + rcr

    # Sort: non-DQ by borda_score asc (tiebreak: tau desc, p_close gap asc), DQ at end
    def _sort_key(r):
        if r["disqualified"]:
            return (1, 9999, -9999, 9999)
        tau = r.get("tau", 0)
        gap = r.get("p_open", 0) - r.get("p_close", 0)
        return (0, r["borda_score"], -tau, gap)

    rows.sort(key=_sort_key)
    return rows
