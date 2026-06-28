#!/usr/bin/env python3
"""Phase DIAG-MFE-MAE — trade geometry + stop level diagnostic (analysis only).

Source: R1-Fixed p=0.65 arm (backtest/results/phase_r1_fixed/sym_p65), 46 trades,
all epg_window_close exits. No backtest run, no param/code change.

Outputs (backtest/results/phase_mfe_mae/):
  trade_base.json, trade_metrics.json, stop_simulation.json,
  charts/chart_01..08*.html, charts/index.html, summary.md
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))

from data.loaders.trades import load_trades

import plotly.graph_objects as go
from plotly.subplots import make_subplots

ARM_DIR = BACKTEST / "results" / "phase_r1_fixed" / "sym_p65"
EVENT_FILE = Path(r"D:\Trading Research\data\val_mdr150_diagnostic.json")
OUT = BACKTEST / "results" / "phase_mfe_mae"
CHARTS = OUT / "charts"
OUT.mkdir(parents=True, exist_ok=True)
CHARTS.mkdir(parents=True, exist_ok=True)

ACTUAL_PF = 2.6584
ACTUAL_CVAR5 = -27.74

OPT1_BUFFERS = [0.00, 0.02, 0.05]
OPT2_X = [0.15, 0.20, 0.25, 0.30, 0.40, 0.50]

REQ = ["entry_t_sec", "entry_price", "exit_t_sec", "exit_price", "pnl_pct", "hold_sec"]


def cvar5(pnl_pct_arr):
    """Mean of most-negative int(0.05*n) trades — same method as runner_rapid."""
    s = np.sort(np.asarray(pnl_pct_arr, dtype=float))
    n = len(s)
    if n == 0:
        return None
    k = max(1, int(0.05 * n))
    return float(np.mean(s[:k]))


def pf(pnl_pct_arr):
    a = np.asarray(pnl_pct_arr, dtype=float)
    w = float(a[a > 0].sum())
    l = float(abs(a[a < 0].sum()))
    if l == 0:
        return None
    return w / l


# ── T1 — load + join ───────────────────────────────────────────────────────────

def t1_load():
    with open(ARM_DIR / "per_trade.json") as f:
        trades = json.load(f)
    with open(EVENT_FILE) as f:
        events = json.load(f)["events"]
    emap = {(e["ticker"], e["date"]): e for e in events}

    missing_fields = [t for t in trades if any(k not in t or t[k] is None for k in REQ)]
    if missing_fields:
        print("T1a HARD STOP — trades missing required fields:")
        for t in missing_fields:
            print("  ", {k: t.get(k) for k in ["ticker", "date"] + REQ})
        sys.exit(1)

    unmatched = [(t["ticker"], t["date"]) for t in trades
                 if (t["ticker"], t["date"]) not in emap]
    if unmatched:
        print("T1b HARD STOP — (ticker,date) not in val sample:", unmatched)
        sys.exit(1)

    base = []
    t1c_excluded = []
    for t in trades:
        e = emap[(t["ticker"], t["date"])]
        gap_move = float(t["entry_price"]) - float(e["prev_close"])
        rec = {
            "ticker": t["ticker"], "date": t["date"],
            "event_idx": t.get("event_idx"),
            "entry_t_sec": float(t["entry_t_sec"]),
            "entry_price": float(t["entry_price"]),
            "exit_t_sec": float(t["exit_t_sec"]),
            "exit_price": float(t["exit_price"]),
            "pnl_pct": float(t["pnl_pct"]),
            "hold_sec": float(t["hold_sec"]),
            "scanner_hit_price": float(e["scanner_hit_price"]),
            "prev_close": float(e["prev_close"]),
            "mom_pct": float(e["mom_pct"]),
            "gap_move": gap_move,
            "win": bool(t["pnl_pct"] > 0),
            "gap_fraction_valid": gap_move > 0,
        }
        if gap_move <= 0:
            t1c_excluded.append((t["ticker"], t["date"], gap_move))
        base.append(rec)

    # trade_base.json excludes the internal mom_pct/gap_fraction_valid? keep them, harmless
    with open(OUT / "trade_base.json", "w") as f:
        json.dump(base, f, indent=2)
    print(f"T1: {len(base)} trades; T1c gap_move<=0 exclusions: {len(t1c_excluded)} {t1c_excluded}")
    return base, t1c_excluded


# ── T2 + T3 — ticks + metrics ───────────────────────────────────────────────────

def t2_t3(base):
    metrics = []
    series = []  # per-trade resampled open-PnL for charts 6/7
    tick_counts = []
    t2a_edges = []
    total_ticks = 0

    for rec in base:
        td = load_trades(rec["ticker"], rec["date"], rec["mom_pct"])
        t_sec = np.asarray(td.t_sec, dtype=float)
        prices = np.asarray(td.prices, dtype=float)
        m = (t_sec >= rec["entry_t_sec"]) & (t_sec <= rec["exit_t_sec"])
        wt = t_sec[m]
        wp = prices[m]
        n = len(wt)
        tick_counts.append(n)
        total_ticks += n

        entry_price = rec["entry_price"]
        gap_move = rec["gap_move"]
        gf_valid = rec["gap_fraction_valid"]
        hold = rec["hold_sec"]

        if n == 0:
            t2a_edges.append((rec["ticker"], rec["date"]))
            mn = mx = entry_price
            t_min = t_max = rec["entry_t_sec"]
        else:
            i_min = int(np.argmin(wp)); i_max = int(np.argmax(wp))
            mn = float(wp[i_min]); mx = float(wp[i_max])
            t_min = float(wt[i_min]); t_max = float(wt[i_max])

        mae_pct = (entry_price - mn) / entry_price
        mfe_pct = (mx - entry_price) / entry_price
        t_mae_sec = t_min - rec["entry_t_sec"]
        t_mfe_sec = t_max - rec["entry_t_sec"]
        t_mae_rel = (t_mae_sec / hold) if hold > 0 else 0.0
        t_mfe_rel = (t_mfe_sec / hold) if hold > 0 else 0.0

        rec_m = {
            **rec,
            "n_ticks_hold": n,
            "min_price": mn, "max_price": mx,
            "t_min_sec": t_min, "t_max_sec": t_max,
            "mae_pct": mae_pct,
            "mae_gap_fraction": ((entry_price - mn) / gap_move) if gf_valid else None,
            "mae_below_scanner": (mn - rec["scanner_hit_price"]) / rec["scanner_hit_price"],
            "t_mae_sec": t_mae_sec, "t_mae_relative": t_mae_rel,
            "mfe_pct": mfe_pct,
            "mfe_gap_fraction": ((mx - entry_price) / gap_move) if gf_valid else None,
            "t_mfe_sec": t_mfe_sec, "t_mfe_relative": t_mfe_rel,
            "mfe_before_mae": bool(t_max < t_min),
            "edge_case_zero_ticks": n == 0,
        }
        metrics.append(rec_m)

        # resample open PnL to 1s grid (forward-fill)
        H = int(np.floor(hold))
        grid = np.arange(0, H + 1)
        if n == 0:
            open_pnl = np.zeros(len(grid))
        else:
            trel = wt - rec["entry_t_sec"]
            idx = np.searchsorted(trel, grid, side="right") - 1
            idx = np.clip(idx, 0, n - 1)
            gp = wp[idx]
            # where grid second precedes first tick, fall back to entry_price
            before = grid < trel[0]
            gp = np.where(before, entry_price, gp)
            open_pnl = (gp - entry_price) / entry_price * 100.0
        series.append({
            "ticker": rec["ticker"], "date": rec["date"],
            "win": rec["win"], "pnl_pct": rec["pnl_pct"], "hold_sec": hold,
            "open_pnl": open_pnl,
        })

    with open(OUT / "trade_metrics.json", "w") as f:
        json.dump(metrics, f, indent=2)

    tc = np.asarray(tick_counts)
    print(f"T2: total ticks across 46 hold windows = {total_ticks}; "
          f"per-trade min={int(tc.min())} max={int(tc.max())} mean={tc.mean():.1f}")
    if t2a_edges:
        print(f"T2a edge cases (zero ticks in hold): {t2a_edges}")
    else:
        print("T2a: no zero-tick edge cases")
    return metrics, series


# ── T4 — stop simulation ─────────────────────────────────────────────────────────

def t4_stops(metrics):
    levels = ([{"type": "Option 1", "param": "buffer", "value": b,
               "label": f"scanner×(1−{b:.2f})"} for b in OPT1_BUFFERS]
              + [{"type": "Option 2", "param": "X", "value": x,
                 "label": f"entry−{x:.2f}×gap"} for x in OPT2_X])

    n_wins = sum(1 for m in metrics if m["win"])
    n_losses = sum(1 for m in metrics if not m["win"])
    out = []
    for lv in levels:
        per_trade = []
        sim_pnls_pct = []
        n_false = n_true = n_fired = 0
        for m in metrics:
            if lv["type"] == "Option 1":
                stop_price = m["scanner_hit_price"] * (1.0 - lv["value"])
            else:
                stop_price = m["entry_price"] - lv["value"] * m["gap_move"]
            fired = m["min_price"] < stop_price
            false_stop = fired and m["win"]
            true_stop = fired and (not m["win"])
            if fired:
                sim_pnl = (stop_price - m["entry_price"]) / m["entry_price"]
                n_fired += 1
            else:
                sim_pnl = m["pnl_pct"] / 100.0
            if false_stop:
                n_false += 1
            if true_stop:
                n_true += 1
            sim_pnls_pct.append(sim_pnl * 100.0)
            per_trade.append({
                "ticker": m["ticker"], "date": m["date"],
                "win": m["win"], "stop_price": stop_price,
                "min_price": m["min_price"],
                "stop_fired": bool(fired),
                "false_stop": bool(false_stop), "true_stop": bool(true_stop),
                "simulated_pnl": sim_pnl, "actual_pnl_pct": m["pnl_pct"],
            })
        sim_pnls_pct = np.asarray(sim_pnls_pct)
        sim_pf = pf(sim_pnls_pct)
        out.append({
            "type": lv["type"], "param": lv["param"], "value": lv["value"],
            "label": lv["label"],
            "n_trades": len(metrics), "n_wins": n_wins, "n_losses": n_losses,
            "stop_hit_rate": n_fired / len(metrics),
            "false_stop_rate": (n_false / n_wins) if n_wins else None,
            "true_stop_rate": (n_true / n_losses) if n_losses else None,
            "n_stop_fired": n_fired, "n_false_stop": n_false, "n_true_stop": n_true,
            "simulated_pf": sim_pf,
            "simulated_pf_null_flag": sim_pf is None,
            "simulated_cvar5_pct": cvar5(sim_pnls_pct),
            "pf_delta": (sim_pf - ACTUAL_PF) if sim_pf is not None else None,
            "per_trade": per_trade,
        })
    with open(OUT / "stop_simulation.json", "w") as f:
        json.dump(out, f, indent=2)

    # consistency check: no-stop reproduction
    actual = np.asarray([m["pnl_pct"] for m in metrics])
    print(f"T4: no-stop check — PF={pf(actual):.4f} (baseline {ACTUAL_PF}), "
          f"CVaR5={cvar5(actual):.4f} (baseline {ACTUAL_CVAR5})")
    print(f"T4: {len(out)} levels; wins={n_wins} losses={n_losses}")
    return out, n_wins, n_losses


if __name__ == "__main__":
    base, t1c = t1_load()
    metrics, series = t2_t3(base)
    stops, n_wins, n_losses = t4_stops(metrics)

    import diag_mfe_mae_charts as ch  # noqa: E402
    ch.build_all(metrics, series, stops, n_wins, n_losses, t1c, CHARTS, OUT,
                 ACTUAL_PF, ACTUAL_CVAR5, OPT2_X)
    print("DIAG-MFE-MAE complete.")
