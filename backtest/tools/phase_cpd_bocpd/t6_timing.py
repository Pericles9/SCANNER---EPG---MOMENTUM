"""
Phase CPD-BOCPD — Task T6
=========================
Per-tick timing benchmark for the BOCPD gate update step. Live-trading feasibility check:
the gate must be fast enough not to bottleneck a single-threaded asyncio loop.

Times ONLY gate.update() (the BOCPD posterior step) across the 100-event val sample using
the winner config (falls back to a representative mid-grid config if no winner yet). Reports
mean / p95 / max µs per tick. Flag (no hard stop) if p95 > 500µs.

Output: results/phase_cpd_bocpd/timing_benchmark.json

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_bocpd.t6_timing
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.gate import ParticipationGate
from tools.phase_cpd.cpd0_t1_traces import _build_active_axis, _compute_wji_active

SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH = REPO_ROOT / "config" / "q_bar_tiers.json"
WINNER_PATH = REPO_ROOT / "results" / "phase_cpd_bocpd" / "bocpd_winner.json"
OUT = REPO_ROOT / "results" / "phase_cpd_bocpd" / "timing_benchmark.json"

SIGMA_FALLBACK = 0.209
EPG_WARMUP = 300.0
PRIOR_MEAN_STD = 1.0
DIR_THRESH_MULT = 1.0
MAX_RUN_LENGTH = 600
P95_FLAG_US = 500.0


def _winner_cfg() -> dict:
    if WINNER_PATH.exists():
        w = json.load(open(WINNER_PATH))
        if w.get("status") != "hard_stop" and "lambda_h" in w:
            return {"config_id": w["config_id"], "lambda_h": w["lambda_h"], "p_enter": w["p_enter"]}
    return {"config_id": "lh0.01_pe0.8", "lambda_h": 0.01, "p_enter": 0.80}  # representative


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    cfg = _winner_cfg()
    print(f"T6 timing — config {cfg['config_id']} (lambda_h={cfg['lambda_h']}, p_enter={cfg['p_enter']})")

    events = json.load(open(SAMPLE_PATH))["events"]
    cache = json.load(open(CACHE_PATH))
    lut = {(r["ticker"], r["date"]): r for r in cache
           if r.get("status") == "ok" and r.get("t_event") is not None and r.get("mu_buy") is not None}
    q_bar_cfg = json.load(open(QBAR_PATH))
    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)

    per_tick_us: list[float] = []
    n_events = 0
    n_post_warmup_ticks = 0

    t_start = time.time()
    for e in events:
        c = lut.get((e["ticker"], e["date"]))
        if c is None:
            continue
        try:
            td = load_trades(e["ticker"], e["date"], e["mom_pct"])
            if td.n_trades < 30:
                continue
            qd = load_quotes(e["ticker"], e["date"], e["mom_pct"])
            if qd is None or qd.n_quotes < 10:
                continue
            ofi = compute_trade_ofi(
                trade_timestamps=td.timestamps, trade_prices=td.prices,
                trade_sizes=td.sizes.astype(np.float64),
                quote_timestamps=qd.timestamps,
                quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
                quote_bid_sizes=qd.bid_sizes.astype(np.float64),
                quote_ask_sizes=qd.ask_sizes.astype(np.float64),
                window_sec=10.0, q_bar_fallback=tier_qbar,
            )
            mask, active_seconds, _, _ = _build_active_axis(td)
            if mask.sum() < 30:
                continue
            prices_a = td.prices[mask]
            sizes_a = td.sizes[mask]
            sides_a = ofi.sides[mask]
            t_sec_a = td.t_sec[mask]
            pos = int(np.searchsorted(t_sec_a, c["t_event"], side="right")) - 1
            t_event_active = float(active_seconds[max(pos, 0)])
            wji, _ = _compute_wji_active(prices_a, sizes_a, sides_a, active_seconds,
                                         t_event_active, c["mu_buy"])
        except Exception as ex:
            print(f"  skip {e['ticker']} {e['date']}: {ex}")
            continue

        gate = ParticipationGate(
            half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=EPG_WARMUP,
            gate_mode="bocpd", lambda_h=cfg["lambda_h"], p_enter=cfg["p_enter"],
            sigma_log_fallback=SIGMA_FALLBACK, prior_mean_std=PRIOR_MEAN_STD,
            dir_thresh_mult=DIR_THRESH_MULT, max_run_length=MAX_RUN_LENGTH,
        )
        gate.activate(t_event_active)
        N = len(wji)
        ta = active_seconds.astype(np.float64)
        wj = wji.astype(np.float64)
        for i in range(N):
            t0 = time.perf_counter()
            gate.update(wji=float(wj[i]), timestamp=float(ta[i]), wji_background=1.0)
            dt_us = (time.perf_counter() - t0) * 1e6
            # Count only post-warmup ticks (the BOCPD posterior step). Warmup ticks just
            # append to a list and are not representative of the live hot path.
            if ta[i] - t_event_active >= EPG_WARMUP and ta[i] - t_event_active >= 0:
                per_tick_us.append(dt_us)
                n_post_warmup_ticks += 1
        n_events += 1

    arr = np.array(per_tick_us, dtype=np.float64)
    stats = {
        "config_id": cfg["config_id"], "lambda_h": cfg["lambda_h"], "p_enter": cfg["p_enter"],
        "n_events": n_events, "n_post_warmup_ticks": int(n_post_warmup_ticks),
        "mean_us_per_tick": float(arr.mean()) if arr.size else None,
        "p50_us_per_tick": float(np.percentile(arr, 50)) if arr.size else None,
        "p95_us_per_tick": float(np.percentile(arr, 95)) if arr.size else None,
        "max_us_per_tick": float(arr.max()) if arr.size else None,
        "p95_flag_threshold_us": P95_FLAG_US,
        "p95_exceeds_flag": bool(arr.size and np.percentile(arr, 95) > P95_FLAG_US),
        "max_run_length": MAX_RUN_LENGTH,
        "note": ("Times gate.update() only (BOCPD posterior step), post-warmup ticks. Single "
                 "Python process, no parallelism. perf_counter overhead included."),
        "wall_clock_sec": round(time.time() - t_start, 1),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        json.dump(stats, f, indent=2)

    print(f"\n  n_events={n_events}  n_post_warmup_ticks={n_post_warmup_ticks}")
    print(f"  mean  = {stats['mean_us_per_tick']:.1f} µs/tick")
    print(f"  p50   = {stats['p50_us_per_tick']:.1f} µs/tick")
    print(f"  p95   = {stats['p95_us_per_tick']:.1f} µs/tick")
    print(f"  max   = {stats['max_us_per_tick']:.1f} µs/tick")
    if stats["p95_exceeds_flag"]:
        print(f"  *** FLAG: p95 {stats['p95_us_per_tick']:.1f}µs > {P95_FLAG_US}µs (informational, no hard stop) ***")
    print(f"  -> {OUT.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
