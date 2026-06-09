"""
Phase CPD — Sub-phase CPD-1, Task T6
=====================================
CUSUM gate 100-event val sweep.

Grid (Cooper-approved, widened beyond proposal defaults given the CPD-0 σ_log-saturation
finding): k ∈ {0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0} × h ∈ {2, 4, 8, 12} = 28 configs.

Pipeline (mirrors the WJI-OPT sweep so metrics are directly comparable):
  per event → load trades/quotes → OFI sides → build halt-adjusted active axis + WJI
  (same as CPD-0 T1) → SF entry trajectory → replay each config's CUSUM gate over the
  active-axis WJI → trades. Entry = rising-edge PASS AND SF qualified; exit = gate close
  (PASS → not-PASS). Gate-close-only design, matching the WJI-OPT baseline.

Metric definitions reuse tools.phase_wji_opt.scorer (compute_metrics / borda_rank), so
PF / CVaR5 / EV / capture_fraction are computed identically to the baseline.

Hard filters (plan T6c): CVaR5 ≥ −10%, n_trades ≥ 60, PF ≥ 1.0.
Winner = highest Borda rank on (capture_fraction, EV, CVaR5); tie → higher CVaR5
(scorer breaks ties by median_pct then n_trades; CVaR5 preference noted in findings).

Escalation (plan):
  - no config passes hard filters  → HARD STOP
  - winner PF < 1.10               → HARD STOP

σ_log is estimated per-event from the warmup window inside the gate; fallback 0.209
(CPD-0). All 100 events have ≥20 warmup ticks, so the fallback is not expected to fire.

Outputs:
  results/phase_cpd/cpd1/cusum_sweep_results.json
  results/phase_cpd/cpd1/cusum_winner.json
  results/phase_cpd/cpd1/cusum_winner_per_year.json

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd.cpd1_t6_sweep
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, _session_ns_bounds
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.gate import ParticipationGate, GateState
from tools.sweep_runner_opt2 import precompute_sf_trajectory, sf_is_qualified_at
from tools.phase_cpd.cpd0_t1_traces import _build_active_axis, _compute_wji_active
from tools.phase_wji_opt.scorer import (
    compute_metrics, compute_per_year, apply_hard_filters, borda_rank,
)

K_GRID = [0.5, 1.0, 2.0, 3.0, 5.0, 8.0, 12.0]
H_GRID = [2, 4, 8, 12]
SIGMA_FALLBACK = 0.209
EPG_WARMUP = 300.0
THRESHOLDS = {"n_trades_floor": 60, "cvar5_floor_pct": -10.0, "pf_floor": 1.0}
PF_HARD_STOP = 1.10

OUT_DIR = REPO_ROOT / "results" / "phase_cpd" / "cpd1"
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH = REPO_ROOT / "config" / "q_bar_tiers.json"
MAX_WORKERS = 8


def build_grid() -> list[dict]:
    """28 configs: k × h. config_id = 'k{k}_h{h}'."""
    return [{"config_id": f"k{k:g}_h{h:g}", "k": float(k), "h": float(h)}
            for k in K_GRID for h in H_GRID]


# ══════════════════════════════════════════════════════════════════════
#  CUSUM gate replay over the active-axis WJI
# ══════════════════════════════════════════════════════════════════════

def _replay_cusum(cfg, wji, t_active, prices_a, ts_ns_a, t_event_active,
                  sf, year, ticker, date) -> list[dict]:
    """Replay one CUSUM config over an event; return trade dicts (gate-close exit)."""
    gate = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=EPG_WARMUP,
        gate_mode="cusum", k=cfg["k"], h=cfg["h"], sigma_log_fallback=SIGMA_FALLBACK,
    )
    gate.activate(t_event_active)

    N = len(wji)
    prices = np.asarray(prices_a, dtype=np.float64)
    max_from = np.maximum.accumulate(prices[::-1])[::-1]  # hindsight reach for avail-move
    use_sf = sf is not None and sf.n_bars > 0

    prev = GateState.INACTIVE
    in_pos = False
    entry_t = entry_price = None
    entry_idx = None
    trades: list[dict] = []

    for i in range(N):
        st = gate.update(wji=float(wji[i]), timestamp=float(t_active[i]), wji_background=1.0)
        if not in_pos:
            if st == GateState.PASS and prev in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL):
                if (not use_sf) or sf_is_qualified_at(sf, int(ts_ns_a[i])):
                    in_pos = True
                    entry_t = float(t_active[i])
                    entry_idx = min(i + 1, N - 1)
                    entry_price = float(prices[entry_idx])
        else:
            if prev == GateState.PASS and st != GateState.PASS:
                exit_price = float(prices[min(i + 1, N - 1)])
                trades.append({
                    "pnl_pct": (exit_price - entry_price) / entry_price * 100.0,
                    "available_move_pct": max(max_from[entry_idx] / entry_price - 1.0, 0.0) * 100.0,
                    "hold_sec": float(t_active[i]) - entry_t,
                    "year": year, "ticker": ticker, "date": date,
                })
                in_pos = False
                entry_t = entry_price = entry_idx = None
        prev = st

    if in_pos:  # close any open position at the last tick
        exit_price = float(prices[N - 1])
        trades.append({
            "pnl_pct": (exit_price - entry_price) / entry_price * 100.0,
            "available_move_pct": max(max_from[entry_idx] / entry_price - 1.0, 0.0) * 100.0,
            "hold_sec": float(t_active[N - 1]) - entry_t,
            "year": year, "ticker": ticker, "date": date,
        })
    return trades


def cusum_sweep_worker(args: dict) -> dict:
    """Per-event: compute active-axis WJI once, replay all 28 configs."""
    ticker, date, mom_pct = args["ticker"], args["date"], args["mom_pct"]
    t_event_raw, mu_buy = args["t_event"], args["mu_buy"]
    q_bar_cfg, configs = args["q_bar_cfg"], args["configs"]
    year = date[:4]
    base = {"ticker": ticker, "date": date, "year": year}

    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return {**base, "status": "skipped", "reason": "insufficient_trades"}
        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return {**base, "status": "skipped", "reason": "insufficient_quotes"}

        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides_full = ofi.sides

        mask, active_seconds, _, _ = _build_active_axis(td)
        if mask.sum() < 30:
            return {**base, "status": "skipped", "reason": "insufficient_active_trades"}

        prices_a = td.prices[mask]
        sizes_a = td.sizes[mask]
        sides_a = sides_full[mask]
        ts_ns_a = td.timestamps[mask]
        t_sec_a = td.t_sec[mask]

        pos = int(np.searchsorted(t_sec_a, t_event_raw, side="right")) - 1
        t_event_active = float(active_seconds[max(pos, 0)])

        wji, _ = _compute_wji_active(prices_a, sizes_a, sides_a, active_seconds,
                                     t_event_active, mu_buy)

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)

        config_results = {
            cfg["config_id"]: _replay_cusum(cfg, wji, active_seconds, prices_a, ts_ns_a,
                                            t_event_active, sf, year, ticker, date)
            for cfg in configs
        }
        return {**base, "status": "ok", "config_results": config_results}

    except Exception as e:
        import traceback
        return {**base, "status": "error", "error": str(e), "traceback": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════
#  Driver
# ══════════════════════════════════════════════════════════════════════

def _write_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp",
                                     delete=False, encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=lambda o: float(o) if isinstance(o, np.floating) else int(o)
                  if isinstance(o, np.integer) else str(o))
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    events = json.load(open(SAMPLE_PATH))["events"]
    cache = json.load(open(CACHE_PATH))
    lut = {(r["ticker"], r["date"]): r for r in cache
           if r.get("status") == "ok" and r.get("t_event") is not None and r.get("mu_buy") is not None}
    q_bar_cfg = json.load(open(QBAR_PATH))
    configs = build_grid()

    work = []
    for e in events:
        c = lut.get((e["ticker"], e["date"]))
        if c is None:
            continue
        work.append({"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
                     "t_event": c["t_event"], "mu_buy": c["mu_buy"],
                     "q_bar_cfg": q_bar_cfg, "configs": configs})

    print(f"T6: {len(work)} events × {len(configs)} configs")
    cfg_ids = [c["config_id"] for c in configs]
    trades_by_cfg = {cid: [] for cid in cfg_ids}
    n_ok = 0
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(cusum_sweep_worker, a) for a in work]
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r["status"] == "ok":
                n_ok += 1
                for cid, trs in r["config_results"].items():
                    trades_by_cfg[cid].extend(trs)
            elif r["status"] == "error":
                print(f"ERROR {r['ticker']} {r['date']}: {r.get('error')}")
            if done % 20 == 0:
                print(f"  {done}/{len(work)} ({time.time()-t0:.0f}s)")

    # Metrics per config
    metrics_by_cfg = {cid: compute_metrics(trades_by_cfg[cid], THRESHOLDS) for cid in cfg_ids}
    survivors = apply_hard_filters(cfg_ids, metrics_by_cfg, THRESHOLDS)
    ranked = borda_rank(survivors, metrics_by_cfg) if survivors else []

    # Sweep results table
    grid_lookup = {c["config_id"]: c for c in configs}
    rows = []
    for cid in cfg_ids:
        m = metrics_by_cfg[cid]
        rows.append({
            "config_id": cid, "k": grid_lookup[cid]["k"], "h": grid_lookup[cid]["h"],
            "pf": m["pf"], "n_trades": m["n_trades"], "cvar5_pct": m["cvar5_pct"],
            "ev": m["ev"], "capture_fraction": m["capture_fraction"],
            "passes_filters": cid in survivors,
            "borda_rank": (ranked.index(cid) + 1) if cid in ranked else None,
        })
    rows.sort(key=lambda r: (r["borda_rank"] is None, r["borda_rank"] if r["borda_rank"] else 999))
    _write_json({"grid": {"k": K_GRID, "h": H_GRID}, "thresholds": THRESHOLDS,
                 "n_events_ok": n_ok, "results": rows}, OUT_DIR / "cusum_sweep_results.json")

    # Escalation + winner
    print(f"\n{'cfg':<10}{'k':>5}{'h':>4}{'PF':>8}{'n':>7}{'CVaR5':>9}{'EV':>9}{'capt':>10}{'pass':>6}{'rank':>5}")
    for r in rows:
        pf = f"{r['pf']:.3f}" if r['pf'] is not None else "—"
        cv = f"{r['cvar5_pct']:.2f}" if r['cvar5_pct'] is not None else "—"
        ev = f"{r['ev']:.3f}" if r['ev'] is not None else "—"
        cf = f"{r['capture_fraction']:.5f}" if r['capture_fraction'] is not None else "—"
        print(f"{r['config_id']:<10}{r['k']:>5g}{r['h']:>4g}{pf:>8}{r['n_trades']:>7}{cv:>9}{ev:>9}{cf:>10}"
              f"{str(r['passes_filters']):>6}{str(r['borda_rank']):>5}")

    if not survivors:
        print("\n*** HARD STOP (T6c): no config passes hard filters. ***")
        _write_json({"status": "hard_stop", "reason": "no_config_passes_filters"},
                    OUT_DIR / "cusum_winner.json")
        return

    winner_id = ranked[0]
    wm = metrics_by_cfg[winner_id]
    print(f"\nWINNER: {winner_id}  PF={wm['pf']:.4f}  n={wm['n_trades']}  "
          f"CVaR5={wm['cvar5_pct']:.2f}  EV={wm['ev']:.4f}  capture={wm['capture_fraction']:.5f}")

    winner_out = {"config_id": winner_id, **grid_lookup[winner_id], "metrics": wm,
                  "n_survivors": len(survivors), "survivors": survivors,
                  "baseline_wji_opt": {"pf": 1.1881, "cvar5_pct": -9.16, "ev": 0.157, "n_trades": 2134}}
    if wm["pf"] is not None and wm["pf"] < PF_HARD_STOP:
        winner_out["escalation"] = f"WINNER PF {wm['pf']:.4f} < {PF_HARD_STOP} — HARD STOP"
        print(f"\n*** HARD STOP (T6c): winner PF {wm['pf']:.4f} < {PF_HARD_STOP}. ***")
    _write_json(winner_out, OUT_DIR / "cusum_winner.json")

    per_year = compute_per_year(trades_by_cfg[winner_id])
    _write_json({"config_id": winner_id, "per_year": per_year},
                OUT_DIR / "cusum_winner_per_year.json")
    print("\nPer-year (winner):")
    for yr, m in per_year.items():
        print(f"  {yr}: PF={m['pf']:.3f}  n={m['n_trades']}  CVaR5={m['cvar5_pct']:.2f}")


if __name__ == "__main__":
    main()
