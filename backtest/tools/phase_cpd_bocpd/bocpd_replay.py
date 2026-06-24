"""
Phase CPD-BOCPD-VIZ — Task T1/T2
=================================
Instrumented replay module for the BOCPD winner config (lh0.01_pe0.6).

Re-runs the full event pipeline (active-axis WJI → BOCPD gate) for a single event
and captures per-tick diagnostic arrays WITHOUT modifying gate.py. Diagnostic data
is read from gate.last_bocpd_debug after each gate.update() call (additive, no
production-path side effects).

Winner config (fixed): lambda_h=0.01, p_enter=0.60, p_exit=0.50, prior_mean_std=1.0,
dir_thresh_mult=1.0, max_run_length=600, sigma_log_fallback=0.209.

Public API
----------
    replay_event_bocpd(args: dict) -> dict
        args keys: ticker, date, mom_pct, t_event, mu_buy, q_bar_cfg
        Returns a replay-result dict (see _RESULT_SCHEMA below); status='error'
        if the event could not be processed.

    run_all_and_cache(out_path: Path) -> dict
        Runs replay_event_bocpd for all 100 val events and writes replay_cache.json.
        Returns {events_total, events_replayed, events_with_trades, errors}.

    Run as main:
        "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_bocpd.bocpd_replay

Output
------
    results/phase_cpd_bocpd/viz/replay_cache.json
"""
from __future__ import annotations

import json
import math
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

# ── Winner config (fixed) ──
WINNER_LAMBDA_H = 0.01
WINNER_P_ENTER = 0.60
WINNER_P_EXIT = 0.50
PRIOR_MEAN_STD = 1.0
DIR_THRESH_MULT = 1.0
MAX_RUN_LENGTH = 600
SIGMA_FALLBACK = 0.209
EPG_WARMUP = 300.0

# ── Data paths ──
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH  = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH   = REPO_ROOT / "config" / "q_bar_tiers.json"
OUT_DIR     = REPO_ROOT / "results" / "phase_cpd_bocpd" / "viz"

MAX_WORKERS = 6


# ══════════════════════════════════════════════════════════════════════
#  OHLCV helper
# ══════════════════════════════════════════════════════════════════════

def _build_ohlcv_10s(ts_ns: np.ndarray, prices: np.ndarray, sizes: np.ndarray) -> list[dict]:
    """Aggregate ticks into 10-second OHLCV bars aligned to the first tick ns."""
    if len(ts_ns) == 0:
        return []
    bin_ns = 10 * 10**9  # 10 seconds in nanoseconds
    t0 = int(ts_ns[0])
    offsets = (ts_ns.astype(np.int64) - t0) // bin_ns
    bars = {}
    for i in range(len(ts_ns)):
        b = int(offsets[i])
        p = float(prices[i])
        v = float(sizes[i])
        if b not in bars:
            bars[b] = {"open": p, "high": p, "low": p, "close": p, "volume": v,
                       "open_ts_ns": t0 + b * bin_ns}
        else:
            bars[b]["high"] = max(bars[b]["high"], p)
            bars[b]["low"] = min(bars[b]["low"], p)
            bars[b]["close"] = p
            bars[b]["volume"] += v
    return [bars[k] for k in sorted(bars)]


# ══════════════════════════════════════════════════════════════════════
#  Core replay function
# ══════════════════════════════════════════════════════════════════════

def replay_event_bocpd(args: dict) -> dict:
    """
    Run instrumented BOCPD replay for one event.

    Per-tick capture (all ticks): ts_ns, wji_raw, wji_log, gate_state.
    Post-warmup only: p_regime, dominant_rl (from gate.last_bocpd_debug).
    Warmup ticks have p_regime=None, dominant_rl=None.

    Returns dict with keys:
        status, ticker, date, t_event_ns, sigma_log, warmup_end_ns,
        ticks, trades, ohlcv_10s
    """
    ticker = args["ticker"]
    date   = args["date"]
    mom_pct = args["mom_pct"]
    t_event_raw = args["t_event"]
    mu_buy = args["mu_buy"]
    q_bar_cfg = args["q_bar_cfg"]
    base = {"ticker": ticker, "date": date}

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

        prices_a = td.prices[mask].astype(np.float64)
        sizes_a  = td.sizes[mask]
        sides_a  = sides_full[mask]
        ts_ns_a  = td.timestamps[mask]            # int64 nanoseconds
        t_sec_a  = td.t_sec[mask]

        pos = int(np.searchsorted(t_sec_a, t_event_raw, side="right")) - 1
        t_event_active = float(active_seconds[max(pos, 0)])

        # T_event in nanoseconds (used as JSON-serialisable identifier)
        t_event_ns = int(ts_ns_a[max(pos, 0)]) if pos >= 0 else int(ts_ns_a[0])

        wji, _ = _compute_wji_active(prices_a, sizes_a, sides_a, active_seconds,
                                     t_event_active, mu_buy)

        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)
        use_sf = sf is not None and sf.n_bars > 0

        gate = ParticipationGate(
            half_life_seconds=300.0, peak_threshold_p=0.65,
            warmup_seconds=EPG_WARMUP, gate_mode="bocpd",
            lambda_h=WINNER_LAMBDA_H, p_enter=WINNER_P_ENTER,
            sigma_log_fallback=SIGMA_FALLBACK,
            prior_mean_std=PRIOR_MEAN_STD,
            dir_thresh_mult=DIR_THRESH_MULT,
            max_run_length=MAX_RUN_LENGTH,
        )
        gate.activate(t_event_active)

        N = len(wji)
        max_from = np.maximum.accumulate(prices_a[::-1])[::-1]

        prev = GateState.INACTIVE
        in_pos = False
        entry_t = entry_price = entry_idx = None
        trades: list[dict] = []
        ticks: list[dict] = []
        warmup_end_ns: Optional[int] = None
        sigma_log_out: Optional[float] = None

        for i in range(N):
            ts_ns_i = int(ts_ns_a[i])
            wji_raw_i = float(wji[i])
            wji_log_i = math.log(wji_raw_i) if wji_raw_i > 0 else float("nan")
            t_active_i = float(active_seconds[i])

            st = gate.update(wji=wji_raw_i, timestamp=t_active_i, wji_background=1.0)
            state_str = st.value  # "WARMUP", "PASS", "FAIL", "INACTIVE"

            tick: dict = {
                "ts_ns": ts_ns_i,
                "t_active": t_active_i,
                "wji_raw": wji_raw_i,
                "wji_log": wji_log_i,
                "gate_state": state_str,
                "p_regime": None,
                "dominant_rl": None,
            }

            if st in (GateState.PASS, GateState.FAIL):
                # Post-warmup: pull diagnostics from gate.last_bocpd_debug
                dbg = gate.last_bocpd_debug
                tick["p_regime"] = float(dbg.get("p_regime", float("nan")))
                tick["dominant_rl"] = int(dbg.get("run_len_mode", 0))
                # Capture sigma_log once (finalised at first post-warmup tick)
                if sigma_log_out is None and gate.sigma_log is not None:
                    sigma_log_out = float(gate.sigma_log)
                    # Find warmup_end_ns: the last WARMUP tick before this one
                    for j in range(i - 1, -1, -1):
                        if ticks[j]["gate_state"] == "WARMUP":
                            warmup_end_ns = int(ticks[j]["ts_ns"])
                            break

            ticks.append(tick)

            # Trade tracking (same rising-edge / gate-close logic as sweep)
            if not in_pos:
                if st == GateState.PASS and prev in (GateState.INACTIVE,
                                                      GateState.WARMUP,
                                                      GateState.FAIL):
                    if (not use_sf) or sf_is_qualified_at(sf, ts_ns_i):
                        in_pos = True
                        entry_idx = min(i + 1, N - 1)
                        entry_t = t_active_i
                        entry_price = float(prices_a[entry_idx])
                        entry_ts_ns = ts_ns_i
            else:
                if prev == GateState.PASS and st != GateState.PASS:
                    exit_idx = min(i + 1, N - 1)
                    exit_price = float(prices_a[exit_idx])
                    exit_ts_ns = int(ts_ns_a[exit_idx])
                    pnl_pct = (exit_price - entry_price) / entry_price * 100.0
                    avail = max(max_from[entry_idx] / entry_price - 1.0, 0.0) * 100.0
                    trades.append({
                        "entry_ts_ns": entry_ts_ns,
                        "exit_ts_ns": exit_ts_ns,
                        "entry_price": entry_price,
                        "exit_price": exit_price,
                        "pnl_pct": pnl_pct,
                        "available_move_pct": avail,
                        "hold_sec": t_active_i - entry_t,
                    })
                    in_pos = False
                    entry_t = entry_price = entry_idx = None

            prev = st

        if in_pos:
            exit_price = float(prices_a[N - 1])
            exit_ts_ns = int(ts_ns_a[N - 1])
            pnl_pct = (exit_price - entry_price) / entry_price * 100.0
            avail = max(max_from[entry_idx] / entry_price - 1.0, 0.0) * 100.0
            trades.append({
                "entry_ts_ns": entry_ts_ns,
                "exit_ts_ns": exit_ts_ns,
                "entry_price": entry_price,
                "exit_price": exit_price,
                "pnl_pct": pnl_pct,
                "available_move_pct": avail,
                "hold_sec": float(active_seconds[N - 1]) - entry_t,
            })

        ohlcv = _build_ohlcv_10s(ts_ns_a, prices_a, sizes_a.astype(np.float64))

        # Compute per-event PF for chart title
        wins  = sum(t["pnl_pct"] for t in trades if t["pnl_pct"] > 0)
        losses = abs(sum(t["pnl_pct"] for t in trades if t["pnl_pct"] < 0))
        event_pf = (wins / losses) if losses > 0 else (float("inf") if wins > 0 else float("nan"))

        return {
            **base,
            "status": "ok",
            "mom_pct": float(mom_pct),
            "t_event_ns": t_event_ns,
            "sigma_log": sigma_log_out if sigma_log_out is not None else SIGMA_FALLBACK,
            "warmup_end_ns": warmup_end_ns,
            "n_ticks": N,
            "n_trades": len(trades),
            "event_pf": event_pf,
            "ticks": ticks,
            "trades": trades,
            "ohlcv_10s": ohlcv,
        }

    except Exception as e:
        import traceback
        return {**base, "status": "error", "error": str(e),
                "traceback": traceback.format_exc()}


# ══════════════════════════════════════════════════════════════════════
#  Batch runner
# ══════════════════════════════════════════════════════════════════════

def _write_json(data, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, suffix=".tmp",
                                     delete=False, encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=_json_default)
        tmp = Path(f.name)
    os.replace(str(tmp), str(path))


def _json_default(obj):
    if isinstance(obj, float) and math.isnan(obj):
        return None
    if isinstance(obj, (np.floating,)):
        return float(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    return str(obj)


def run_all_and_cache(out_path: Optional[Path] = None) -> dict:
    """Replay all 100 val events with the winner config; write replay_cache.json."""
    if out_path is None:
        out_path = OUT_DIR / "replay_cache.json"

    events = json.load(open(SAMPLE_PATH))["events"]
    cache_raw = json.load(open(CACHE_PATH))
    lut = {(r["ticker"], r["date"]): r for r in cache_raw
           if r.get("status") == "ok"
           and r.get("t_event") is not None
           and r.get("mu_buy") is not None}
    q_bar_cfg = json.load(open(QBAR_PATH))

    work = []
    for e in events:
        c = lut.get((e["ticker"], e["date"]))
        if c is None:
            continue
        work.append({"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
                     "t_event": c["t_event"], "mu_buy": c["mu_buy"],
                     "q_bar_cfg": q_bar_cfg})

    print(f"T2: replaying {len(work)} events with winner config "
          f"lambda_h={WINNER_LAMBDA_H} p_enter={WINNER_P_ENTER}")

    results: dict = {}
    errors: list = []
    n_ok = n_with_trades = 0
    t0 = time.time()
    done = 0

    with ProcessPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(replay_event_bocpd, a): (a["ticker"], a["date"]) for a in work}
        for fut in as_completed(futs):
            r = fut.result()
            done += 1
            key = f"{r['ticker']}_{r['date']}"
            if r["status"] == "ok":
                n_ok += 1
                if r["n_trades"] > 0:
                    n_with_trades += 1
                # Store only events with trades in the cache (charts are only needed for those)
                # but store ALL ok events so T2 knows the full picture
                results[key] = r
            elif r["status"] == "error":
                errors.append({"ticker": r["ticker"], "date": r["date"],
                                "error": r.get("error"), "traceback": r.get("traceback")})
                print(f"  ERROR {r['ticker']} {r['date']}: {r.get('error')}")
            else:
                # skipped — still record in results for completeness
                results[key] = {"ticker": r["ticker"], "date": r["date"],
                                 "status": r["status"], "reason": r.get("reason"),
                                 "n_trades": 0}
            if done % 20 == 0:
                print(f"  {done}/{len(work)} ({time.time()-t0:.0f}s)")

    total = len(work)
    err_rate = len(errors) / total if total > 0 else 0.0
    print(f"  done: {n_ok} ok, {len(errors)} errors, {total-n_ok-len(errors)} skipped "
          f"({time.time()-t0:.0f}s)")
    print(f"  events_with_trades={n_with_trades}, error_rate={err_rate:.1%}")

    # Escalation check: error rate > 5%
    if err_rate > 0.05:
        print(f"\n*** HARD STOP: replay error rate {err_rate:.1%} > 5% ***")
        for e in errors:
            print(f"  {e['ticker']} {e['date']}: {e['error']}")
            if e.get("traceback"):
                print(e["traceback"][-800:])
        raise RuntimeError(f"Replay error rate {err_rate:.1%} > 5% — hard stop")

    _write_json({
        "winner_config": {"lambda_h": WINNER_LAMBDA_H, "p_enter": WINNER_P_ENTER,
                          "p_exit": WINNER_P_EXIT, "prior_mean_std": PRIOR_MEAN_STD,
                          "dir_thresh_mult": DIR_THRESH_MULT, "max_run_length": MAX_RUN_LENGTH},
        "n_events_total": total,
        "n_ok": n_ok,
        "n_with_trades": n_with_trades,
        "n_errors": len(errors),
        "error_rate": round(err_rate, 4),
        "errors": errors,
        "events": results,
    }, out_path)
    print(f"  → {out_path}")
    return {"events_total": total, "events_ok": n_ok,
            "events_with_trades": n_with_trades, "n_errors": len(errors)}


# ══════════════════════════════════════════════════════════════════════
#  Escalation checks (T2b)
# ══════════════════════════════════════════════════════════════════════

def check_escalations(results: dict) -> list[str]:
    """
    Run per-spec escalation checks on the replay results.
    Returns a list of triggered escalation strings (empty = no escalations).
    """
    escalations = []
    for key, r in results.items():
        if r.get("status") != "ok" or r.get("n_trades", 0) == 0:
            continue
        ticks = r.get("ticks", [])
        post_warmup = [t for t in ticks if t["gate_state"] in ("PASS", "FAIL")]
        if not post_warmup:
            continue

        # Check 1: wji_log constant
        logs = [t["wji_log"] for t in post_warmup
                if t["wji_log"] is not None and not (isinstance(t["wji_log"], float) and math.isnan(t["wji_log"]))]
        if logs and len(set(round(v, 8) for v in logs)) == 1:
            escalations.append(
                f"ESCALATION: wji_log is constant for {key} — signal pipeline broken"
            )

        # Check 2: p_regime never exceeds p_enter on a traded event
        p_regimes = [t["p_regime"] for t in post_warmup if t["p_regime"] is not None]
        if p_regimes and max(p_regimes) < WINNER_P_ENTER:
            escalations.append(
                f"ESCALATION: p_regime never exceeds p_enter={WINNER_P_ENTER} on {key} "
                f"(max={max(p_regimes):.4f}) but event has trades"
            )
    return escalations


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    stats = run_all_and_cache()
    print(f"\nT2 summary: {stats['events_total']} events total, "
          f"{stats['events_ok']} ok, {stats['events_with_trades']} with trades, "
          f"{stats['n_errors']} errors")

    # Load and run escalation checks
    cache_path = OUT_DIR / "replay_cache.json"
    cache = json.load(open(cache_path))
    escalations = check_escalations(cache["events"])
    if escalations:
        print("\n*** ESCALATION CHECKS TRIGGERED ***")
        for e in escalations:
            print(f"  {e}")
        raise RuntimeError("Escalation checks failed — see above")
    else:
        print("  Escalation checks: OK (no wji_log constant, no p_regime-never-above-p_enter)")


if __name__ == "__main__":
    main()
