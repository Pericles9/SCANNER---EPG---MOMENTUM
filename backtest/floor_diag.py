#!/usr/bin/env python3
"""
EPG Floor Calibration Diagnostic (T2a)
=======================================

Plots lambda_V, running peak, current PASS threshold, background_WJI,
and candidate floor lines (C * bg_WJI) per event.

Usage:
  D:\Trading Research\.venv\Scripts\python.exe floor_diag.py --random-sample 100 --seed 42 -C 3 5 8 12

Output: results/phase_wji_decay/floor_diag/{event}.html  +  index.html
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE.parent))   # scanner-epg-momentum/ for backtest.X imports
sys.path.insert(0, str(_HERE))          # backtest/ for data/, core/, runner direct imports

from data.loaders.trades import load_trades, list_events, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from runner import _hawkes_replay_with_refit

# ── EPG constants (match runner.py) ──
EPG_K = 5
EPG_TAU = 300.0
EPG_P = 0.65
EPG_WARMUP = 300.0
COLD_START_SIZE = 1000

OUT_DIR = _HERE / "results" / "phase_wji_decay" / "floor_diag"


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


def replay_event(ticker, date, mom_pct, hawkes_median, q_bar_cfg, per_event_params):
    """Run Hawkes + anchor + gate replay and return arrays for plotting."""
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

    anchor = EventAnchor(lambda_ref=global_lambda_ref, k_multiplier=EPG_K)
    if lref_epg > 0:
        anchor.set_lambda_ref(lref_epg)
    gate = ParticipationGate(half_life_seconds=EPG_TAU,
                             peak_threshold_p=EPG_P,
                             warmup_seconds=EPG_WARMUP)

    lambda_v = np.zeros(N)
    running_peak = np.zeros(N)
    threshold = np.zeros(N)
    states = []
    t_event_sec = None
    t_event_idx = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and t_event_sec is None:
            gate.activate(t_ev)
            t_event_sec = t_ev
            t_event_idx = i
        dv = float(td.prices[i]) * float(td.sizes[i])
        st = gate.update(dv, td.t_sec[i])
        states.append(st)
        lambda_v[i] = gate.lambda_v
        running_peak[i] = gate.lambda_v_peak
        threshold[i] = gate.threshold

    if t_event_sec is None:
        return None

    # dbar window (a): first COLD_START_SIZE trades
    n_cold = min(COLD_START_SIZE, N)
    dv_cold = td.prices[:n_cold].astype(np.float64) * td.sizes[:n_cold].astype(np.float64)
    dbar_cold = float(dv_cold.mean()) if n_cold > 0 else 0.0
    bg_wji_algebraic = lref_epg * dbar_cold

    # dbar window (b): trades strictly before t_event
    if t_event_idx > 0:
        dv_pre = td.prices[:t_event_idx].astype(np.float64) * td.sizes[:t_event_idx].astype(np.float64)
        dbar_pretev = float(dv_pre.mean()) if len(dv_pre) > 0 else dbar_cold
    else:
        dbar_pretev = dbar_cold

    # bg_WJI direct: median of lambda_V over back half of cold-start window
    bg_gate = ParticipationGate(half_life_seconds=EPG_TAU,
                                peak_threshold_p=EPG_P,
                                warmup_seconds=0.0)
    bg_gate.activate(td.t_sec[0])
    bg_series = np.zeros(n_cold)
    for i in range(n_cold):
        dv = float(td.prices[i]) * float(td.sizes[i])
        bg_gate.update(dv, td.t_sec[i])
        bg_series[i] = bg_gate.lambda_v
    half = n_cold // 2
    bg_wji_direct = float(np.median(bg_series[half:])) if n_cold > 1 else float(bg_series[-1])

    # PASS windows for shading
    pass_windows = []
    open_i = None
    for i in range(N):
        if states[i] == GateState.PASS and open_i is None:
            open_i = i
        elif states[i] != GateState.PASS and open_i is not None:
            pass_windows.append((open_i, i))
            open_i = None
    if open_i is not None:
        pass_windows.append((open_i, N - 1))

    return {
        "ticker": ticker, "date": date, "mom_pct": mom_pct,
        "t_sec": td.t_sec,
        "prices": td.prices,
        "lambda_v": lambda_v,
        "running_peak": running_peak,
        "threshold": threshold,
        "t_event_sec": t_event_sec,
        "t_event_idx": t_event_idx,
        "lref_epg": lref_epg,
        "dbar_cold": dbar_cold,
        "dbar_pretev": dbar_pretev,
        "bg_wji_algebraic": bg_wji_algebraic,
        "bg_wji_direct": bg_wji_direct,
        "pass_windows": pass_windows,
    }


def make_chart(d, c_values, output_path):
    t = d["t_sec"]
    prices = d["prices"]
    bg = d["bg_wji_algebraic"]

    # Build 1-minute candlesticks (bucket = floor(t/60)*60 seconds)
    buckets = (np.floor(t / 60.0) * 60.0).astype(np.float64)
    unique_buckets = np.unique(buckets)
    candle_o, candle_h, candle_l, candle_c = [], [], [], []
    for b in unique_buckets:
        mask = buckets == b
        bp = prices[mask].astype(np.float64)
        candle_o.append(float(bp[0]))
        candle_h.append(float(bp.max()))
        candle_l.append(float(bp.min()))
        candle_c.append(float(bp[-1]))

    fig = make_subplots(
        rows=2, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.05,
        row_heights=[0.45, 0.55],
    )

    # Panel 1: price candlesticks
    fig.add_trace(
        go.Candlestick(
            x=unique_buckets.tolist(),
            open=candle_o, high=candle_h, low=candle_l, close=candle_c,
            name="price",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
        ),
        row=1, col=1,
    )

    x0, x1 = float(t[0]), float(t[-1])

    # Panel 2: lambda_V
    fig.add_trace(
        go.Scatter(x=t, y=d["lambda_v"], mode="lines",
                   line=dict(color="#0097a7", width=1.2), name="lambda_V"),
        row=2, col=1,
    )

    # Panel 2: WJI λ_ref (background level, flat)
    fig.add_trace(
        go.Scatter(x=[x0, x1], y=[bg, bg], mode="lines",
                   line=dict(color="#5d4037", width=1.5), name="WJI λ_ref"),
        row=2, col=1,
    )

    # Panel 2: floor candidates
    palette = ["#d32f2f", "#f57c00", "#7b1fa2", "#1976d2", "#388e3c", "#c2185b", "#f9a825"]
    for j, C in enumerate(c_values):
        floor = C * bg
        fig.add_trace(
            go.Scatter(x=[x0, x1], y=[floor, floor], mode="lines",
                       line=dict(color=palette[j % len(palette)], width=1.2, dash="dash"),
                       name=f"floor C={C}"),
            row=2, col=1,
        )

    # t_event vertical line — spans both panels via shared x
    fig.add_vline(x=d["t_event_sec"], line_color="#000", line_width=1,
                  line_dash="dash", opacity=0.4)

    fig.update_layout(
        title=(f"{d['ticker']} {d['date']}  (mom {d['mom_pct']:.0f}%)   "
               f"lref={d['lref_epg']:.3f} t/s   dbar_a=${d['dbar_cold']:,.0f}   "
               f"dbar_b=${d['dbar_pretev']:,.0f}   "
               f"bg_WJI=${bg:,.0f}/s"),
        template="plotly_white",
        height=720,
        legend=dict(font=dict(size=10)),
    )
    fig.update_yaxes(title_text="price", row=1, col=1)
    fig.update_yaxes(title_text="lambda_V (dollars/sec)", type="log", row=2, col=1)
    fig.update_xaxes(rangeslider_visible=False, row=1, col=1)
    fig.update_xaxes(title_text="seconds from session start", row=2, col=1)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(output_path), include_plotlyjs="cdn")


def write_index(rows, output_path):
    rows_sorted = sorted(rows, key=lambda r: r["bg_wji"], reverse=True)
    html = ["<html><head><meta charset='utf-8'><title>Floor Diag Index</title>",
            "<style>body{font-family:system-ui;margin:24px}"
            "table{border-collapse:collapse}td,th{border:1px solid #ccc;padding:6px 10px;"
            "text-align:right}th{background:#f5f5f5}a{text-decoration:none}"
            "td.l{text-align:left}</style></head><body>",
            "<h2>EPG Floor Calibration — event index</h2>",
            "<table><tr><th class='l'>Event</th><th>mom%</th><th>lref (t/s)</th>"
            "<th>dbar_a ($)</th><th>dbar_b ($)</th><th>bg_WJI ($/s)</th>"
            "<th>algeb/direct ratio</th></tr>"]
    for r in rows_sorted:
        ratio = (r["bg_wji"] / r["bg_direct"]) if r["bg_direct"] else float("nan")
        flag = " style='background:#ffe0e0'" if (ratio < 0.7 or ratio > 1.4) else ""
        html.append(
            f"<tr{flag}><td class='l'><a href='{r['file']}'>{r['ticker']} {r['date']}</a></td>"
            f"<td>{r['mom_pct']:.0f}</td><td>{r['lref']:.3f}</td>"
            f"<td>{r['dbar_a']:,.0f}</td><td>{r['dbar_b']:,.0f}</td>"
            f"<td>{r['bg_wji']:,.0f}</td><td>{ratio:.2f}</td></tr>")
    html.append("</table><p>Red rows: algebraic vs direct bg_WJI diverge >30% "
                "(thin-tape events). dbar_a = first 1000 trades; "
                "dbar_b = trades before T_event.</p></body></html>")
    output_path.write_text("\n".join(html), encoding="utf-8")


def _build_val_sample(n=100, seed=42):
    """Replicate the year-stratified 100-event val sample used by runner.py."""
    import json as _json
    from pathlib import Path as _Path
    boundary_path = _HERE / "config" / "holdout_boundary.json"
    with open(boundary_path) as f:
        boundary = _json.load(f)
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ticker")
    ap.add_argument("--date")
    ap.add_argument("--random-sample", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--val-sample", action="store_true", default=False,
                    help="Use the standard 100-event val sample (seed=42, stratified)")
    ap.add_argument("--min-mom", type=float, default=30.0)
    ap.add_argument("-C", nargs="+", type=float, default=[3, 5, 8, 12],
                    help="candidate floor multipliers")
    args = ap.parse_args()

    hawkes_median, q_bar_cfg, per_event_params = _load_configs()

    if args.ticker and args.date:
        events = [{"ticker": args.ticker, "date": args.date, "mom_pct": 50.0}]
    elif args.val_sample:
        events = _build_val_sample(n=100, seed=42)
        print(f"Using standard 100-event val sample ({len(events)} events)")
    else:
        catalog = list_events(min_mom=args.min_mom, require_date=True)
        random.seed(args.seed)
        n = args.random_sample or 20
        events = random.sample(catalog, min(n, len(catalog)))

    print(f"Generating floor diagnostics for {len(events)} events  C={args.C}")
    rows = []
    for k, ev in enumerate(events):
        tk, dt = ev["ticker"], ev["date"]
        try:
            d = replay_event(tk, dt, ev["mom_pct"],
                             hawkes_median, q_bar_cfg, per_event_params)
        except Exception as e:
            print(f"  [{k+1}] {tk} {dt}  ERROR: {e}")
            continue
        if d is None:
            print(f"  [{k+1}] {tk} {dt}  skipped (no T_event / insufficient data)")
            continue
        fname = f"floor_{tk}_{dt}.html"
        make_chart(d, args.C, OUT_DIR / fname)
        rows.append({"ticker": tk, "date": dt, "mom_pct": d["mom_pct"],
                     "lref": d["lref_epg"], "dbar_a": d["dbar_cold"],
                     "dbar_b": d["dbar_pretev"],
                     "bg_wji": d["bg_wji_algebraic"], "bg_direct": d["bg_wji_direct"],
                     "file": fname})
        print(f"  [{k+1}] {tk} {dt}  bg_WJI=${d['bg_wji_algebraic']:,.0f}/s  "
              f"dbar_a=${d['dbar_cold']:,.0f}  dbar_b=${d['dbar_pretev']:,.0f}")

    if rows:
        write_index(rows, OUT_DIR / "index.html")
        print(f"\nDone. {len(rows)} charts + index in: {OUT_DIR}")
    else:
        print("\nNo charts produced.")


if __name__ == "__main__":
    main()
