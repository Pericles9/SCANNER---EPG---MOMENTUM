#!/usr/bin/env python3
"""
Phase WJI-Decay — T1: dbar window comparison + selection criterion
==================================================================

Compares two candidate dbar windows:
  (a) first COLD_START_SIZE=1000 trades
  (b) trades strictly before t_event

Selection criterion (T1b):
  Choose window (b) if, on events where t_event fires INSIDE the cold-start
  window (t_event_idx < COLD_START_SIZE), window-(a) dbar >= 1.5x window-(b)
  dbar for more than 20% of those events.  Otherwise use window (a).

Output: results/phase_wji_decay/t1_dbar_decision.json

Run:
  D:\Trading Research\.venv\Scripts\python.exe tools/phase_wji_decay/t1_dbar_analysis.py
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

import numpy as np

_HERE = Path(__file__).resolve().parent.parent.parent   # = backtest/
sys.path.insert(0, str(_HERE.parent))   # scanner-epg-momentum/ for backtest.X imports
sys.path.insert(0, str(_HERE))          # backtest/ for data/, core/, runner direct imports

from data.loaders.trades import load_trades, list_events, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from runner import _hawkes_replay_with_refit

EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000

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
    """Replicate the year-stratified 100-event val sample used by runner.py."""
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


def analyse_event(ticker, date, mom_pct, hawkes_median, q_bar_cfg, per_event_params):
    """Return per-event dbar comparison dict or None if skipped."""
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

    # lambda_ref for gate floor: cold-start mu_buy + mu_sell
    lref_epg = global_lambda_ref
    if cold_start_params is not None:
        c = cold_start_params.mu_buy + cold_start_params.mu_sell
        if c > 0:
            lref_epg = c

    # Find t_event
    anchor = EventAnchor(lambda_ref=global_lambda_ref, k_multiplier=EPG_K)
    if lref_epg > 0:
        anchor.set_lambda_ref(lref_epg)
    t_event_idx = None
    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and t_event_idx is None:
            t_event_idx = i
            break

    if t_event_idx is None:
        return None

    # Window (a): first COLD_START_SIZE trades
    n_cold = min(COLD_START_SIZE, N)
    dv_a = td.prices[:n_cold].astype(np.float64) * td.sizes[:n_cold].astype(np.float64)
    dbar_a = float(dv_a.mean())

    # Window (b): trades strictly before t_event
    if t_event_idx > 0:
        dv_b = td.prices[:t_event_idx].astype(np.float64) * td.sizes[:t_event_idx].astype(np.float64)
        dbar_b = float(dv_b.mean()) if len(dv_b) > 0 else dbar_a
        n_pretev = t_event_idx
    else:
        dbar_b = dbar_a
        n_pretev = 0

    fired_inside_cold_start = t_event_idx < COLD_START_SIZE

    bg_wji_a = lref_epg * dbar_a
    bg_wji_b = lref_epg * dbar_b

    return {
        "ticker": ticker,
        "date": date,
        "t_event_idx": int(t_event_idx),
        "n_trades": int(N),
        "n_pretev": int(n_pretev),
        "fired_inside_cold_start": bool(fired_inside_cold_start),
        "dbar_a": float(dbar_a),
        "dbar_b": float(dbar_b),
        "bg_wji_a": float(bg_wji_a),
        "bg_wji_b": float(bg_wji_b),
        "lref_epg": float(lref_epg),
        "contamination": bool(fired_inside_cold_start and dbar_a >= 1.5 * dbar_b),
    }


def main():
    print("Phase WJI-Decay T1: dbar window analysis")
    print("=" * 60)

    hawkes_median, q_bar_cfg, per_event_params = _load_configs()
    events = _build_val_sample(n=100, seed=42)
    print(f"100-event val sample loaded ({len(events)} events)")

    rows = []
    skipped = 0
    for k, ev in enumerate(events):
        try:
            r = analyse_event(ev["ticker"], ev["date"], ev["mom_pct"],
                              hawkes_median, q_bar_cfg, per_event_params)
        except Exception as e:
            print(f"  [{k+1:3d}] {ev['ticker']} {ev['date']}  ERROR: {e}")
            skipped += 1
            continue
        if r is None:
            print(f"  [{k+1:3d}] {ev['ticker']} {ev['date']}  skipped")
            skipped += 1
            continue
        rows.append(r)
        print(f"  [{k+1:3d}] {ev['ticker']} {ev['date']}  "
              f"t_event_idx={r['t_event_idx']:4d}  "
              f"inside_cold={r['fired_inside_cold_start']}  "
              f"dbar_a=${r['dbar_a']:8,.0f}  dbar_b=${r['dbar_b']:8,.0f}  "
              f"contaminated={r['contamination']}")

    print(f"\nProcessed: {len(rows)} events  skipped: {skipped}")

    # T1b selection criterion
    inside_cold = [r for r in rows if r["fired_inside_cold_start"]]
    contaminated = [r for r in inside_cold if r["contamination"]]

    n_inside = len(inside_cold)
    n_contaminated = len(contaminated)
    contamination_rate = n_contaminated / n_inside if n_inside > 0 else 0.0

    threshold_pct = 0.20
    if n_inside == 0:
        selected_window = "a"
        selection_reason = "no events fire inside cold-start window; window (a) by default"
    elif contamination_rate > threshold_pct:
        selected_window = "b"
        selection_reason = (
            f"contamination rate {contamination_rate:.1%} > {threshold_pct:.0%} threshold "
            f"({n_contaminated}/{n_inside} events firing inside cold-start have "
            f"dbar_a >= 1.5x dbar_b) — window (b) selected to avoid burst contamination"
        )
    else:
        selected_window = "a"
        selection_reason = (
            f"contamination rate {contamination_rate:.1%} <= {threshold_pct:.0%} threshold "
            f"({n_contaminated}/{n_inside} events) — window (a) selected "
            f"for robustness on thin tape"
        )

    print(f"\nT1 selection:")
    print(f"  Events firing inside cold-start window: {n_inside}/{len(rows)}")
    print(f"  Of those, contaminated (dbar_a >= 1.5x dbar_b): {n_contaminated}")
    print(f"  Contamination rate: {contamination_rate:.1%}")
    print(f"  SELECTED WINDOW: ({selected_window}) — {selection_reason}")

    # T1a: escalation check — algebraic vs direct divergence > 30% on > 25% of events
    # (bg_wji_direct not computed here — that's in floor_diag.py)
    # We check dbar_a vs dbar_b divergence across all events
    dbar_ratios = [(r["dbar_a"] / r["dbar_b"]) if r["dbar_b"] > 0 else 1.0 for r in rows]
    divergent = sum(1 for ratio in dbar_ratios if ratio > 1.3 or ratio < 0.7)
    divergence_rate = divergent / len(rows) if rows else 0.0
    print(f"\n  dbar_a/dbar_b divergence >30%: {divergent}/{len(rows)} = {divergence_rate:.1%}")

    # Statistics
    dbar_a_vals = [r["dbar_a"] for r in rows]
    dbar_b_vals = [r["dbar_b"] for r in rows]
    bg_wji_a_vals = [r["bg_wji_a"] for r in rows]
    bg_wji_b_vals = [r["bg_wji_b"] for r in rows]

    import statistics
    stats = {
        "dbar_a": {
            "mean": statistics.mean(dbar_a_vals),
            "median": statistics.median(dbar_a_vals),
            "p10": float(np.percentile(dbar_a_vals, 10)),
            "p90": float(np.percentile(dbar_a_vals, 90)),
        },
        "dbar_b": {
            "mean": statistics.mean(dbar_b_vals),
            "median": statistics.median(dbar_b_vals),
            "p10": float(np.percentile(dbar_b_vals, 10)),
            "p90": float(np.percentile(dbar_b_vals, 90)),
        },
        "bg_wji_a": {
            "mean": statistics.mean(bg_wji_a_vals),
            "median": statistics.median(bg_wji_a_vals),
            "p10": float(np.percentile(bg_wji_a_vals, 10)),
            "p90": float(np.percentile(bg_wji_a_vals, 90)),
        },
        "bg_wji_b": {
            "mean": statistics.mean(bg_wji_b_vals),
            "median": statistics.median(bg_wji_b_vals),
            "p10": float(np.percentile(bg_wji_b_vals, 10)),
            "p90": float(np.percentile(bg_wji_b_vals, 90)),
        },
    }

    decision = {
        "selected_window": selected_window,
        "selection_reason": selection_reason,
        "n_events_analysed": len(rows),
        "n_events_skipped": skipped,
        "n_events_firing_inside_cold_start": n_inside,
        "n_contaminated": n_contaminated,
        "contamination_rate": round(contamination_rate, 4),
        "contamination_threshold": threshold_pct,
        "criterion": "window (b) if contamination_rate > 0.20, else window (a)",
        "dbar_a_divergence_rate_vs_b": round(divergence_rate, 4),
        "stats": stats,
        "per_event": rows,
    }

    out_path = OUT_DIR / "t1_dbar_decision.json"
    with open(out_path, "w") as f:
        json.dump(decision, f, indent=2)
    print(f"\nWritten: {out_path}")

    return selected_window, decision


if __name__ == "__main__":
    main()
