#!/usr/bin/env python3
"""
Phase WJI-Decay — T2b: C calibration support sweep
====================================================

For each candidate C in {2, 3, 4, 5, 6, 8, 10}, simulate the decaying-peak
floor gate on the 100-event val sample and compute:

  - pass_fraction      fraction of post-warmup ticks in PASS state
  - median_first_fail  median time (s) from warmup-end to first FAIL
  - bind_pct           % of events where floor actually binds (floor > decayed
                       peak at any tick)
  - reopen_cycles      total PASS→FAIL→PASS reopen cycles across sample

Uses tau_peak = 600s (fixed, just to make the floor observable).
Reads dbar window from t1_dbar_decision.json.

Output: results/phase_wji_decay/t2_c_sweep.json

Run:
  D:\Trading Research\.venv\Scripts\python.exe tools/phase_wji_decay/t2b_c_sweep.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path
from typing import Optional

import numpy as np

_HERE = Path(__file__).resolve().parent.parent.parent   # = backtest/
sys.path.insert(0, str(_HERE.parent))   # scanner-epg-momentum/ for backtest.X imports
sys.path.insert(0, str(_HERE))          # backtest/ for data/, core/, runner direct imports

from data.loaders.trades import load_trades, list_events, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from runner import _hawkes_replay_with_refit

EPG_K = 5
EPG_TAU = 300.0
EPG_P_OPEN = 0.65
EPG_P_CLOSE = 0.30   # asymmetric hysteresis (Phase EPG-GRT config)
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000
TAU_PEAK_FIXED = 600.0   # fixed for T2b to isolate floor effect
LN2 = math.log(2)

C_CANDIDATES = [2, 3, 4, 5, 6, 8, 10]

OUT_DIR = _HERE / "results" / "phase_wji_decay"
OUT_DIR.mkdir(parents=True, exist_ok=True)


def _load_configs():
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)
    phase_a_path = _HERE / "results" / "phase_a" / "production_fit_results.json"
    per_event_params = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    per_event_params[(r["ticker"], r["date"])] = r["final_params"]
    return hawkes_median, q_bar_cfg, per_event_params


def _build_val_sample(n=100, seed=42):
    with open(CONFIG_DIR / "holdout_boundary.json") as f:
        boundary = json.load(f)
    val_start = boundary["val_split_start_date"]
    test_start = boundary["test_split_start_date"]
    all_events = list_events(min_mom=50.0, require_date=True)
    events = [e for e in all_events if val_start <= e["date"] < test_start]
    rng = random.Random(seed)
    by_year = {}
    for e in events:
        yr = e["date"][:4]
        by_year.setdefault(yr, []).append(e)
    year_counts = {y: len(evs) for y, evs in by_year.items()}
    total = sum(year_counts.values())
    alloc = {y: int(n * cnt / total) for y, cnt in year_counts.items()}
    remainder = n - sum(alloc.values())
    for y in sorted(year_counts, key=year_counts.get, reverse=True):
        if remainder <= 0:
            break
        alloc[y] += 1
        remainder -= 1
    sampled = []
    for y in sorted(by_year):
        n_y = min(alloc[y], len(by_year[y]))
        sampled.extend(rng.sample(by_year[y], n_y))
    return sorted(sampled, key=lambda e: (e["date"], e["ticker"]))


def _load_t1_decision() -> str:
    """Return 'a' or 'b' from t1_dbar_decision.json, defaulting to 'a'."""
    t1_path = OUT_DIR / "t1_dbar_decision.json"
    if not t1_path.exists():
        print("WARNING: t1_dbar_decision.json not found — defaulting to window (a)")
        return "a"
    with open(t1_path) as f:
        d = json.load(f)
    return d.get("selected_window", "a")


def replay_event_arrays(ticker, date, mom_pct, hawkes_median, q_bar_cfg,
                         per_event_params, dbar_window: str):
    """
    Run Hawkes + anchor replay and return arrays needed for floor simulation.
    Returns None if event is skipped.

    dbar_window: 'a' = first COLD_START_SIZE trades, 'b' = before t_event
    """
    td = load_trades(ticker, date, mom_pct)
    qd = load_quotes(ticker, date, mom_pct)
    if td is None or qd is None or td.n_trades < 30 or qd.n_quotes < 10:
        return None

    fp = per_event_params.get((ticker, date), hawkes_median)
    rho = hawkes_median.get("rho", 0.99)
    rho_E = rho
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

    lam_buy_out = np.zeros(N, dtype=np.float64)
    lam_sell_out = np.zeros(N, dtype=np.float64)
    E_out = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    n_base_out = np.zeros(N, dtype=np.float64)

    global_lambda_ref = fp["mu_buy"] + fp["mu_sell"]
    per_event_lref = compute_lambda_ref_per_event(ticker, date)
    lambda_ref = (global_lambda_ref
                  if (math.isnan(per_event_lref) or per_event_lref <= 0)
                  else per_event_lref)

    cold_start_params = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=sides,
        rho=rho, lambda_ref=lambda_ref,
        init_params=fp, rho_E=rho_E,
        lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
        E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
    )
    lambda_hat = lam_buy_out + lam_sell_out

    lref_epg = global_lambda_ref
    if cold_start_params is not None:
        c = cold_start_params.mu_buy + cold_start_params.mu_sell
        if c > 0:
            lref_epg = c

    # Find t_event and replay lambda_v
    anchor = EventAnchor(lambda_ref=global_lambda_ref, k_multiplier=EPG_K)
    if lref_epg > 0:
        anchor.set_lambda_ref(lref_epg)

    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P_OPEN,
        warmup_seconds=EPG_WARMUP,
        p_open=EPG_P_OPEN,
        p_close=EPG_P_CLOSE,
    )

    lambda_v = np.zeros(N)
    t_event_sec = None
    t_event_idx = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and t_event_sec is None:
            gate.activate(t_ev)
            t_event_sec = t_ev
            t_event_idx = i
        dv = float(td.prices[i]) * float(td.sizes[i])
        gate.update(dv, td.t_sec[i])
        lambda_v[i] = gate.lambda_v

    if t_event_sec is None:
        return None

    # Compute dbar
    n_cold = min(COLD_START_SIZE, N)
    dv_cold = td.prices[:n_cold].astype(np.float64) * td.sizes[:n_cold].astype(np.float64)
    dbar_a = float(dv_cold.mean()) if n_cold > 0 else 0.0

    if dbar_window == "b" and t_event_idx > 0:
        dv_b = td.prices[:t_event_idx].astype(np.float64) * td.sizes[:t_event_idx].astype(np.float64)
        dbar = float(dv_b.mean()) if len(dv_b) > 0 else dbar_a
    else:
        dbar = dbar_a

    return {
        "t_sec": td.t_sec,
        "lambda_v": lambda_v,
        "t_event_sec": t_event_sec,
        "t_event_idx": int(t_event_idx),
        "lref_epg": float(lref_epg),
        "dbar": float(dbar),
        "N": N,
    }


def simulate_floor_gate(
    t_sec: np.ndarray,
    lambda_v: np.ndarray,
    t_event_sec: float,
    floor: float,
    tau_peak: float,
    p_open: float,
    p_close: float,
    warmup_sec: float,
) -> dict:
    """
    Simulate the decaying-peak floor gate on pre-computed lambda_v series.

    Returns per-event metrics:
      pass_ticks, total_post_warmup_ticks,
      first_fail_t, floor_bound_ticks, reopen_cycles
    """
    N = len(t_sec)
    decay_rate_peak = LN2 / tau_peak

    peak_eff = 0.0
    last_t = t_event_sec
    in_pass = False
    first_pass_t = None
    first_fail_t = None

    pass_ticks = 0
    post_warmup_ticks = 0
    floor_bound_ticks = 0
    reopen_cycles = 0
    prev_was_pass = False

    for i in range(N):
        t = t_sec[i]
        if t < t_event_sec:
            continue

        dt = max(0.0, t - last_t)

        # Decay peak toward floor
        if dt > 0 and peak_eff > 0:
            decayed = peak_eff * math.exp(-decay_rate_peak * dt)
            peak_eff = max(decayed, floor)
        elif peak_eff == 0.0:
            peak_eff = floor

        # Check if floor is binding (floor >= decayed value before ratchet)
        if peak_eff <= floor + 1e-12:
            floor_bound_ticks += 1

        # Ratchet up on new high
        if lambda_v[i] > peak_eff:
            peak_eff = lambda_v[i]

        last_t = t

        # Warmup
        if t - t_event_sec < warmup_sec:
            continue

        post_warmup_ticks += 1

        if in_pass:
            if lambda_v[i] < p_close * peak_eff:
                in_pass = False
        else:
            if peak_eff > 0 and lambda_v[i] >= p_open * peak_eff:
                in_pass = True
                if first_pass_t is None:
                    first_pass_t = t

        if in_pass:
            pass_ticks += 1
            if not prev_was_pass and first_pass_t is not None:
                # FAIL → PASS reopen (not counting initial open)
                if first_fail_t is not None:
                    reopen_cycles += 1
        else:
            if prev_was_pass and first_fail_t is None:
                first_fail_t = t

        prev_was_pass = in_pass

    return {
        "pass_ticks": pass_ticks,
        "post_warmup_ticks": post_warmup_ticks,
        "first_fail_t": first_fail_t,  # None if never failed after first PASS
        "floor_bound_ticks": floor_bound_ticks,
        "reopen_cycles": reopen_cycles,
        "t_event_sec": t_event_sec,
        "warmup_end_t": t_event_sec + warmup_sec,
        "session_end_t": float(t_sec[-1]),
    }


def main():
    print("Phase WJI-Decay T2b: C calibration sweep")
    print("=" * 60)

    hawkes_median, q_bar_cfg, per_event_params = _load_configs()
    events = _build_val_sample(n=100, seed=42)
    dbar_window = _load_t1_decision()
    print(f"100-event val sample  dbar_window=({dbar_window})  tau_peak={TAU_PEAK_FIXED}s")
    print(f"C candidates: {C_CANDIDATES}")

    # Replay all events once (expensive — Hawkes refit)
    print("\nReplaying events...")
    replays = {}
    skipped = 0
    for k, ev in enumerate(events):
        try:
            r = replay_event_arrays(ev["ticker"], ev["date"], ev["mom_pct"],
                                    hawkes_median, q_bar_cfg, per_event_params,
                                    dbar_window=dbar_window)
        except Exception as e:
            print(f"  [{k+1:3d}] {ev['ticker']} {ev['date']}  ERROR: {e}")
            skipped += 1
            continue
        if r is None:
            skipped += 1
            continue
        replays[(ev["ticker"], ev["date"])] = r
        if (k + 1) % 10 == 0:
            print(f"  [{k+1:3d}/{len(events)}] replayed {len(replays)} events so far")

    print(f"\nReplayed: {len(replays)}  skipped: {skipped}")

    # Original gate pass fraction (no floor, tau_peak=inf) for reference
    orig_pass_ticks = 0
    orig_post_warmup_ticks = 0
    orig_first_fail_list = []
    for r in replays.values():
        m = simulate_floor_gate(
            t_sec=r["t_sec"], lambda_v=r["lambda_v"],
            t_event_sec=r["t_event_sec"],
            floor=0.0, tau_peak=1e9,   # floor disabled = original gate
            p_open=EPG_P_OPEN, p_close=EPG_P_CLOSE,
            warmup_sec=EPG_WARMUP,
        )
        orig_pass_ticks += m["pass_ticks"]
        orig_post_warmup_ticks += m["post_warmup_ticks"]
        if m["first_fail_t"] is not None:
            orig_first_fail_list.append(m["first_fail_t"] - m["warmup_end_t"])
        else:
            orig_first_fail_list.append(m["session_end_t"] - m["warmup_end_t"])

    orig_pass_frac = orig_pass_ticks / orig_post_warmup_ticks if orig_post_warmup_ticks else 0.0
    orig_median_ff = float(np.median(orig_first_fail_list)) if orig_first_fail_list else 0.0

    print(f"\nOriginal gate (no floor): pass_frac={orig_pass_frac:.3f}  "
          f"median_first_fail={orig_median_ff:.0f}s")

    # Sweep C
    results = []
    print("\nC sweep:")
    print(f"{'C':>4}  {'pass_frac':>10}  {'med_first_fail':>14}  "
          f"{'bind_pct':>9}  {'reopen':>8}  {'pass_frac_delta':>15}")
    print("-" * 70)

    for C in C_CANDIDATES:
        all_pass_ticks = 0
        all_post_warmup_ticks = 0
        first_fail_times = []
        bind_events = 0
        total_reopen_cycles = 0
        n_events = 0

        for (ticker, date), r in replays.items():
            floor = C * r["lref_epg"] * r["dbar"]
            m = simulate_floor_gate(
                t_sec=r["t_sec"], lambda_v=r["lambda_v"],
                t_event_sec=r["t_event_sec"],
                floor=floor, tau_peak=TAU_PEAK_FIXED,
                p_open=EPG_P_OPEN, p_close=EPG_P_CLOSE,
                warmup_sec=EPG_WARMUP,
            )
            all_pass_ticks += m["pass_ticks"]
            all_post_warmup_ticks += m["post_warmup_ticks"]
            if m["floor_bound_ticks"] > 0:
                bind_events += 1
            total_reopen_cycles += m["reopen_cycles"]
            n_events += 1

            if m["first_fail_t"] is not None:
                ff_delta = m["first_fail_t"] - m["warmup_end_t"]
            else:
                ff_delta = m["session_end_t"] - m["warmup_end_t"]
            first_fail_times.append(ff_delta)

        pass_frac = all_pass_ticks / all_post_warmup_ticks if all_post_warmup_ticks else 0.0
        median_ff = float(np.median(first_fail_times)) if first_fail_times else 0.0
        bind_pct = 100.0 * bind_events / n_events if n_events else 0.0
        pf_delta = pass_frac - orig_pass_frac

        print(f"{C:>4}  {pass_frac:>10.3f}  {median_ff:>14.0f}  "
              f"{bind_pct:>8.1f}%  {total_reopen_cycles:>8}  {pf_delta:>+15.3f}")

        results.append({
            "C": C,
            "floor_mult": C,
            "tau_peak_sec": TAU_PEAK_FIXED,
            "dbar_window": dbar_window,
            "pass_fraction": round(pass_frac, 4),
            "pass_fraction_delta_vs_original": round(pf_delta, 4),
            "median_time_to_first_fail_sec": round(median_ff, 1),
            "orig_median_first_fail_sec": round(orig_median_ff, 1),
            "median_first_fail_delta_sec": round(median_ff - orig_median_ff, 1),
            "bind_pct": round(bind_pct, 2),
            "bind_events": bind_events,
            "reopen_cycles": total_reopen_cycles,
            "n_events": n_events,
        })

    print("\nReference (no floor):")
    print(f"  pass_fraction={orig_pass_frac:.3f}  "
          f"median_first_fail={orig_median_ff:.0f}s")

    output = {
        "original_gate": {
            "pass_fraction": round(orig_pass_frac, 4),
            "median_time_to_first_fail_sec": round(orig_median_ff, 1),
            "description": "ParticipationGate with p_open=0.65, p_close=0.30, no floor"
        },
        "sweep": results,
        "params": {
            "tau_peak_fixed_sec": TAU_PEAK_FIXED,
            "p_open": EPG_P_OPEN,
            "p_close": EPG_P_CLOSE,
            "warmup_sec": EPG_WARMUP,
            "dbar_window": dbar_window,
            "n_events": len(replays),
            "note": "tau_peak is fixed at 600s for T2b purely to make floor observable; "
                    "tau_peak calibration happens in T4 after C is selected by Cooper"
        },
        "action_required": "Cooper selects C from the sweep table above before T3 implementation",
    }

    out_path = OUT_DIR / "t2_c_sweep.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWritten: {out_path}")

    # Print bg_wji magnitude summary to help Cooper calibrate expectations
    bg_vals = [r["lref_epg"] * r["dbar"] for r in replays.values()]
    print(f"\nbg_WJI magnitude across {len(bg_vals)} events:")
    print(f"  median=${np.median(bg_vals):,.0f}/s  "
          f"p10=${np.percentile(bg_vals, 10):,.0f}/s  "
          f"p90=${np.percentile(bg_vals, 90):,.0f}/s")
    print("  (floor at C=5 would be 5× these values)")


if __name__ == "__main__":
    main()
