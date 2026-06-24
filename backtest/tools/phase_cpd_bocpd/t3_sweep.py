"""
Phase CPD-BOCPD — Task T3 (+T4 winner selection)
================================================
BOCPD gate 100-event val sweep. Grid: lambda_h ∈ {0.005,0.01,0.02,0.05} ×
p_enter ∈ {0.60,0.70,0.80,0.90} = 16 configs.

Mirrors tools.phase_cpd.cpd1_t6_sweep EXACTLY except the gate accumulator: same event
list (results/phase_wji_poc/quality_sample_val.json), same active-axis WJI extraction
(Option A: WJI_background ≡ 1.0, WJI_log = log(WJI)), same SF entry gate, same gate-close
exit, same WJI-OPT scorer (compute_metrics / borda_rank). Only `gate_mode` changes:
"cusum" -> "bocpd" (directional surge-aware; see core/epg/gate.py::_update_bocpd).

Fixed BOCPD model constants (NOT swept, documented in the gate): prior_mean_std=1.0,
dir_thresh_mult=1.0, max_run_length=600. p_exit = p_enter - 0.10 (fixed hysteresis gap).

Records per config: config_id, lambda_h, p_enter, pf, n_trades, cvar5_pct, ev,
capture_fraction, pass_fraction.

Eligibility (T4 Borda): n_trades >= 60 AND pf >= 1.0. Borda over (capture_fraction, ev,
cvar5_pct); tie -> higher cvar5_pct. Escalation handled by the caller / report (CVaR5 < -10%
or winner PF < 1.10 -> HARD STOP).

Outputs:
  results/phase_cpd_bocpd/bocpd_sweep_results.json
  results/phase_cpd_bocpd/bocpd_winner.json
  results/phase_cpd_bocpd/bocpd_winner_per_year.json

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_bocpd.t3_sweep
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

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

LAMBDA_GRID = [0.005, 0.01, 0.02, 0.05]
P_ENTER_GRID = [0.60, 0.70, 0.80, 0.90]
SIGMA_FALLBACK = 0.209
EPG_WARMUP = 300.0
# Fixed BOCPD model constants (not swept).
PRIOR_MEAN_STD = 1.0
DIR_THRESH_MULT = 1.0
MAX_RUN_LENGTH = 600

THRESHOLDS = {"n_trades_floor": 60, "cvar5_floor_pct": -10.0, "pf_floor": 1.0}
PF_HARD_STOP = 1.10
CVAR5_HARD_STOP = -10.0

OUT_DIR = REPO_ROOT / "results" / "phase_cpd_bocpd"
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH = REPO_ROOT / "config" / "q_bar_tiers.json"
MAX_WORKERS = 8


def build_grid() -> list[dict]:
    """16 configs: lambda_h × p_enter. config_id = 'lh{lambda_h}_pe{p_enter}'."""
    return [{"config_id": f"lh{lh:g}_pe{pe:g}", "lambda_h": float(lh), "p_enter": float(pe)}
            for lh in LAMBDA_GRID for pe in P_ENTER_GRID]


# ══════════════════════════════════════════════════════════════════════
#  BOCPD gate replay over the active-axis WJI
# ══════════════════════════════════════════════════════════════════════

def _replay_bocpd(cfg, wji, t_active, prices_a, ts_ns_a, t_event_active,
                  sf, year, ticker, date) -> tuple[list[dict], int, int]:
    """
    Replay one BOCPD config over an event.

    Returns (trades, n_pass_ticks, n_post_warmup_ticks). Entry = rising-edge PASS AND SF
    qualified; exit = gate close (PASS -> not-PASS). Gate-close-only design, matching the
    CPD-1 / WJI-OPT baseline exactly.
    """
    gate = ParticipationGate(
        half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=EPG_WARMUP,
        gate_mode="bocpd", lambda_h=cfg["lambda_h"], p_enter=cfg["p_enter"],
        sigma_log_fallback=SIGMA_FALLBACK, prior_mean_std=PRIOR_MEAN_STD,
        dir_thresh_mult=DIR_THRESH_MULT, max_run_length=MAX_RUN_LENGTH,
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
    n_pass = 0
    n_post_warmup = 0

    for i in range(N):
        st = gate.update(wji=float(wji[i]), timestamp=float(t_active[i]), wji_background=1.0)
        if st in (GateState.PASS, GateState.FAIL):
            n_post_warmup += 1
            if st == GateState.PASS:
                n_pass += 1
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
    return trades, n_pass, n_post_warmup


def bocpd_sweep_worker(args: dict) -> dict:
    """Per-event: compute active-axis WJI once, replay all 16 configs."""
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

        config_results = {}
        tick_stats = {}
        for cfg in configs:
            trs, n_pass, n_pw = _replay_bocpd(cfg, wji, active_seconds, prices_a, ts_ns_a,
                                              t_event_active, sf, year, ticker, date)
            config_results[cfg["config_id"]] = trs
            tick_stats[cfg["config_id"]] = (n_pass, n_pw)
        return {**base, "status": "ok", "config_results": config_results, "tick_stats": tick_stats}

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

    print(f"T3: {len(work)} events × {len(configs)} configs (BOCPD)")
    cfg_ids = [c["config_id"] for c in configs]
    trades_by_cfg = {cid: [] for cid in cfg_ids}
    pass_ticks_by_cfg = {cid: 0 for cid in cfg_ids}
    pw_ticks_by_cfg = {cid: 0 for cid in cfg_ids}
    n_ok = 0
    errors = []
    t0 = time.time()
    done = 0
    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = [ex.submit(bocpd_sweep_worker, a) for a in work]
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            if r["status"] == "ok":
                n_ok += 1
                for cid, trs in r["config_results"].items():
                    trades_by_cfg[cid].extend(trs)
                    n_pass, n_pw = r["tick_stats"][cid]
                    pass_ticks_by_cfg[cid] += n_pass
                    pw_ticks_by_cfg[cid] += n_pw
            elif r["status"] == "error":
                errors.append({"ticker": r["ticker"], "date": r["date"], "error": r.get("error")})
                print(f"ERROR {r['ticker']} {r['date']}: {r.get('error')}")
            if done % 20 == 0:
                print(f"  {done}/{len(work)} ({time.time()-t0:.0f}s)")

    print(f"  done: {n_ok} ok, {len(errors)} errored ({time.time()-t0:.0f}s)")

    # Metrics per config
    metrics_by_cfg = {cid: compute_metrics(trades_by_cfg[cid], THRESHOLDS) for cid in cfg_ids}
    survivors = apply_hard_filters(cfg_ids, metrics_by_cfg, THRESHOLDS)
    # T4 eligibility = n_trades >= 60 AND pf >= 1.0 (subset of the survivors filter, which
    # also includes CVaR5 >= -10%). Borda is run on the n_trades/pf-eligible set per plan T4.
    eligible = [cid for cid in cfg_ids
                if metrics_by_cfg[cid]["n_trades"] is not None
                and metrics_by_cfg[cid]["n_trades"] >= THRESHOLDS["n_trades_floor"]
                and metrics_by_cfg[cid]["pf"] is not None
                and metrics_by_cfg[cid]["pf"] >= THRESHOLDS["pf_floor"]]
    ranked = borda_rank(eligible, metrics_by_cfg) if eligible else []

    grid_lookup = {c["config_id"]: c for c in configs}

    def _pass_frac(cid):
        pw = pw_ticks_by_cfg[cid]
        return (pass_ticks_by_cfg[cid] / pw) if pw > 0 else None

    rows = []
    for cid in cfg_ids:
        m = metrics_by_cfg[cid]
        rows.append({
            "config_id": cid,
            "lambda_h": grid_lookup[cid]["lambda_h"], "p_enter": grid_lookup[cid]["p_enter"],
            "p_exit": round(grid_lookup[cid]["p_enter"] - 0.10, 4),
            "pf": m["pf"], "n_trades": m["n_trades"], "cvar5_pct": m["cvar5_pct"],
            "ev": m["ev"], "capture_fraction": m["capture_fraction"],
            "pass_fraction": _pass_frac(cid),
            "median_pct": m["median_pct"], "max_loss_pct": m["max_loss_pct"],
            "eligible": cid in eligible,
            "passes_hard_filters": cid in survivors,
            "borda_rank": (ranked.index(cid) + 1) if cid in ranked else None,
        })
    rows.sort(key=lambda r: (r["cvar5_pct"] is None, -(r["cvar5_pct"] or -1e9)))  # CVaR5 desc

    _write_json({
        "grid": {"lambda_h": LAMBDA_GRID, "p_enter": P_ENTER_GRID},
        "fixed_model_constants": {
            "prior_mean_std": PRIOR_MEAN_STD, "dir_thresh_mult": DIR_THRESH_MULT,
            "max_run_length": MAX_RUN_LENGTH, "hysteresis_gap": 0.10,
            "sigma_log_fallback": SIGMA_FALLBACK,
        },
        "thresholds": THRESHOLDS, "n_events_ok": n_ok, "n_errored": len(errors),
        "errors": errors, "results": rows,
    }, OUT_DIR / "bocpd_sweep_results.json")

    # Print sweep table (CVaR5 desc)
    print(f"\n{'cfg':<14}{'lh':>7}{'pe':>6}{'PF':>8}{'n':>7}{'CVaR5':>9}{'EV':>9}"
          f"{'capt':>10}{'passf':>8}{'elig':>6}{'rank':>5}")
    for r in rows:
        pf = f"{r['pf']:.3f}" if r['pf'] is not None else "—"
        cv = f"{r['cvar5_pct']:.2f}" if r['cvar5_pct'] is not None else "—"
        ev = f"{r['ev']:.3f}" if r['ev'] is not None else "—"
        cf = f"{r['capture_fraction']:.5f}" if r['capture_fraction'] is not None else "—"
        pfr = f"{r['pass_fraction']:.4f}" if r['pass_fraction'] is not None else "—"
        print(f"{r['config_id']:<14}{r['lambda_h']:>7g}{r['p_enter']:>6g}{pf:>8}{r['n_trades']:>7}"
              f"{cv:>9}{ev:>9}{cf:>10}{pfr:>8}{str(r['eligible']):>6}{str(r['borda_rank']):>5}")

    # ── T4 winner + escalation ──
    if not eligible:
        print("\n*** HARD STOP (T4): no eligible config (all fail n_trades>=60 or PF>=1.0). ***")
        _write_json({"status": "hard_stop", "reason": "no_eligible_config",
                     "thresholds": THRESHOLDS}, OUT_DIR / "bocpd_winner.json")
        return

    winner_id = ranked[0]
    wm = metrics_by_cfg[winner_id]
    # Borda detail table for eligible configs
    borda_detail = _borda_detail(eligible, metrics_by_cfg, ranked)

    escalations = []
    if wm["cvar5_pct"] is not None and wm["cvar5_pct"] < CVAR5_HARD_STOP:
        escalations.append(f"WINNER CVaR5 {wm['cvar5_pct']:.2f}% < {CVAR5_HARD_STOP}% — HARD STOP")
    if wm["pf"] is not None and wm["pf"] < PF_HARD_STOP:
        escalations.append(f"WINNER PF {wm['pf']:.4f} < {PF_HARD_STOP} — HARD STOP")

    winner_out = {
        "config_id": winner_id, **grid_lookup[winner_id],
        "p_exit": round(grid_lookup[winner_id]["p_enter"] - 0.10, 4),
        "metrics": {**wm, "pass_fraction": _pass_frac(winner_id)},
        "n_eligible": len(eligible), "eligible": eligible,
        "n_survivors_all_filters": len(survivors), "survivors_all_filters": survivors,
        "borda_detail": borda_detail,
        "escalations": escalations,
        "cpd1_best": {"config": "k12_h8", "pf": 1.1176, "cvar5_pct": -30.55,
                      "ev": 0.6468, "n_trades": 143, "capture_fraction": 0.01645},
        "wji_opt_baseline": {"config": "p065_single", "pf": 1.1881, "cvar5_pct": -9.16,
                             "ev": 0.157, "n_trades": 2134},
    }
    _write_json(winner_out, OUT_DIR / "bocpd_winner.json")

    print(f"\nWINNER (Borda): {winner_id}  PF={wm['pf']:.4f}  n={wm['n_trades']}  "
          f"CVaR5={wm['cvar5_pct']:.2f}  EV={wm['ev']:.4f}  "
          f"capture={wm['capture_fraction']:.5f}  pass_frac={_pass_frac(winner_id):.4f}")
    if escalations:
        for e in escalations:
            print(f"  *** {e} ***")

    per_year = compute_per_year(trades_by_cfg[winner_id])
    _write_json({"config_id": winner_id, "per_year": per_year},
                OUT_DIR / "bocpd_winner_per_year.json")
    print("\nPer-year (winner):")
    for yr, m in per_year.items():
        cv = f"{m['cvar5_pct']:.2f}" if m['cvar5_pct'] is not None else "—"
        pf = f"{m['pf']:.3f}" if m['pf'] is not None else "—"
        print(f"  {yr}: PF={pf}  n={m['n_trades']}  CVaR5={cv}")


def _borda_detail(eligible, metrics_by_cfg, ranked):
    """Per-axis Borda points + total for each eligible config (for the report)."""
    import math as _m

    def _get(cid, key):
        v = metrics_by_cfg[cid].get(key)
        if v is None or (isinstance(v, float) and _m.isnan(v)):
            return -float("inf")
        return float(v)

    axes = ["capture_fraction", "ev", "cvar5_pct"]
    # rank within eligible per axis (higher = better → higher points)
    detail = {cid: {} for cid in eligible}
    totals = {cid: 0.0 for cid in eligible}
    for axis in axes:
        order = sorted(eligible, key=lambda c: _get(c, axis), reverse=True)
        n = len(order)
        for pos, cid in enumerate(order):
            pts = n - 1 - pos
            detail[cid][f"{axis}_rank"] = pos + 1
            detail[cid][f"{axis}_points"] = pts
            totals[cid] += pts
    out = []
    for cid in ranked:
        out.append({"config_id": cid, "borda_total": totals[cid],
                    "final_rank": ranked.index(cid) + 1, **detail[cid]})
    return out


if __name__ == "__main__":
    main()
