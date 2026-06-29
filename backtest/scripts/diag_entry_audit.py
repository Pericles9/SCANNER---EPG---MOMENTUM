"""
Phase DIAG-ENTRY T1-T4 — entry-failure audit for the R3 disabled arm.

Read-only. For each of the 80 processed events, replay the FULL-session Hawkes/gate
pipeline exactly as runner_rapid does (full replay needed so the cold-start MLE matches
the runner — truncating at scanner+300 would change lambda_ref and could disagree with R3),
then analyse the entry window [scanner, scanner+300] and classify the failure mode.

Writes (phase_diag_entry/): entry_audit.json, entry_audit_excluded.json,
failure_breakdown.json, prescan_history.json, chart_selection.json.
"""
from __future__ import annotations
import json
import math
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))
sys.path.insert(0, str(BACKTEST.parent))  # repo root — runner_rapid imports `backtest.setup_filter`

from data.loaders.trades import load_trades, _session_ns_bounds, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate
from runner_rapid import (
    _hawkes_replay_with_refit, _build_halt_intervals,
    EPG_K, EPG_TAU, EPG_WARMUP, HALT_GAP_THRESHOLD,
)
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND

R3 = BACKTEST / "results" / "phase_r3"
OUT = BACKTEST / "results" / "phase_diag_entry"
OUT.mkdir(parents=True, exist_ok=True)
SAMPLE = BACKTEST / "data" / "val_r3_stratified.json"
P_OPEN = P_CLOSE = 0.65
TAU_PEAK = 600.0; GATE_C = 1.5; GATE_MODE = "peak"
MAX_LAG = 300.0

EXCLUDED = [
    {"ticker": "SKYE", "date": "2023-12-06", "status": "ERROR", "reason": "missing quotes.parquet"},
    {"ticker": "SKYE", "date": "2024-02-27", "status": "ERROR", "reason": "missing quotes.parquet"},
    {"ticker": "PSIX", "date": "2024-07-02", "status": "ERROR", "reason": "missing quotes.parquet"},
    {"ticker": "ODVWZ", "date": "2024-01-29", "status": "SKIPPED", "reason": "no_t_event"},
    {"ticker": "FBYDW", "date": "2024-06-28", "status": "SKIPPED", "reason": "no_t_event"},
    {"ticker": "ALCYW", "date": "2023-12-06", "status": "SKIPPED", "reason": "no_t_event"},
]


def audit_one(task):
    tk, dt = task["ticker"], task["date"]
    try:
        td = load_trades(tk, dt, task["mom_pct"])
        qd = load_quotes(tk, dt, task["mom_pct"])
        pc = get_prev_close(tk, dt)
        if td is None or qd is None or pc is None:
            return {"ticker": tk, "date": dt, "error": "load_failed"}
        N = td.n_trades
        t_sec = np.asarray(td.t_sec, dtype=float)
        scanner_t = (int(task["scanner_hit_ts_ns"]) - int(td.timestamps[0])) / NS_PER_SECOND
        deadline = scanner_t + MAX_LAG
        fp = task["fp"]

        tier = task["q_bar_cfg"].get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64), quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64), window_sec=10.0, q_bar_fallback=tier)
        sides = ofi.sides
        halts = _build_halt_intervals(td)
        lb = np.zeros(N); ls = np.zeros(N); E = np.zeros(N); Ed = np.zeros(N); nb = np.zeros(N)
        dv = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
        glref = fp["mu_buy"] + fp["mu_sell"]
        plref = compute_lambda_ref_per_event(tk, dt)
        lref = glref if (math.isnan(plref) or plref <= 0) else plref
        cold = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=task["rho"], lambda_ref=lref, init_params=fp,
            rho_E=task["rho"], lam_buy_out=lb, lam_sell_out=ls, E_out=E, Edot_out=Ed,
            n_base_out=nb, dv_arr=dv, halt_intervals=halts or None)
        lambda_hat = lb + ls

        # anchor lambda_ref exactly as runner: global, overridden by cold-start if >0
        lambda_ref_eff = glref
        if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
            lambda_ref_eff = cold.mu_buy + cold.mu_sell

        anchor = EventAnchor(lambda_ref=glref, k_multiplier=EPG_K)
        if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
            anchor.set_lambda_ref(cold.mu_buy + cold.mu_sell)
        gate = ParticipationGate(half_life_seconds=EPG_TAU, peak_threshold_p=P_OPEN,
                                 warmup_seconds=EPG_WARMUP, gate_mode=GATE_MODE,
                                 tau_peak=TAU_PEAK, C=GATE_C, p_open=P_OPEN, p_close=P_CLOSE)
        t_event = None
        states = []
        for i in range(N):
            ev = anchor.update(lambda_hat[i], t_sec[i])
            if ev is not None and t_event is None:
                t_event = float(t_sec[i])
                gate.activate(ev)          # MUST activate gate on anchor fire (matches runner)
            gt = t_sec[i]
            if i > 0 and halts and t_sec[i] - t_sec[i-1] > HALT_GAP_THRESHOLD:
                for hs, he in halts:
                    if t_sec[i-1] < he and t_sec[i] > hs:
                        gt = t_sec[i-1] + 1e-6; break
            states.append(gate.update(float(td.prices[i]) * float(td.sizes[i]), gt).name)

        states = np.array(states)
        # window analysis
        n_before = int(np.sum(t_sec < scanner_t))
        idx_sc = int(np.searchsorted(t_sec, scanner_t, side="left"))
        idx_sc = min(idx_sc, N - 1)
        gate_at_sc = states[idx_sc]
        win = (t_sec >= scanner_t) & (t_sec <= deadline)
        pass_win = win & (states == "PASS")
        any_pass = bool(pass_win.any())
        t_first_pass = float(t_sec[pass_win][0]) if any_pass else None
        pass_all = states == "PASS"
        first_pass_overall = float(t_sec[pass_all][0]) if pass_all.any() else None

        anchor_fired = t_event is not None
        t_ev_rel = (t_event - scanner_t) if anchor_fired else None
        t_warm_end = (t_event + EPG_WARMUP) if anchor_fired else None
        warm_after = (t_warm_end > deadline) if anchor_fired else None

        r3_traded = (tk, dt) in task["traded"]
        # classify
        if r3_traded:
            reason = "TRADED"
        elif not anchor_fired:
            reason = "ANCHOR_NEVER_FIRED"
        elif t_ev_rel > MAX_LAG:
            reason = "ANCHOR_LATE"
        elif warm_after:
            reason = "WARMUP_AT_DEADLINE"
        elif first_pass_overall is None:
            reason = "NEVER_PASS_IN_WINDOW"
        elif (first_pass_overall - scanner_t) > MAX_LAG:
            reason = "PASS_TOO_LATE"
        else:
            reason = "NEVER_PASS_IN_WINDOW"  # warmup done, no pass in window, pass exists in window? -> handled by consistency

        return {
            "ticker": tk, "date": dt, "stratum": task["stratum"],
            "gap_pct_at_hit": task["gap_pct_at_hit"], "is_first_appearance": task["is_first"],
            "n_trades_before_scanner": n_before, "n_ticks_session_total": N,
            "pct_trades_before_scanner": round(n_before / N, 4) if N else None,
            "lambda_ref_cold_start": round(float(lambda_ref_eff), 6),
            "anchor_threshold": round(float(EPG_K * lambda_ref_eff), 6),
            "anchor_fired": anchor_fired,
            "t_event_anchor_sec": round(t_event, 3) if anchor_fired else None,
            "t_event_anchor_relative_sec": round(t_ev_rel, 3) if anchor_fired else None,
            "gate_state_at_scanner_hit": str(gate_at_sc),
            "gate_at_scanner_hit_runner": task["gate_runner"],
            "t_warmup_end_sec": round(t_warm_end, 3) if anchor_fired else None,
            "warmup_ends_after_deadline": warm_after,
            "any_pass_in_entry_window": any_pass,
            "t_first_pass_in_window_sec": round(t_first_pass, 3) if t_first_pass is not None else None,
            "t_first_pass_relative_sec": round(t_first_pass - scanner_t, 3) if t_first_pass is not None else None,
            "first_pass_overall_relative_sec": round(first_pass_overall - scanner_t, 3) if first_pass_overall is not None else None,
            "scanner_hit_t_sec": round(scanner_t, 3),
            "entry_failure_reason": reason,
            "r3_traded": r3_traded,
            "audit_would_trade": any_pass,
            "consistent": (any_pass == r3_traded),
        }
    except Exception as e:
        import traceback
        return {"ticker": tk, "date": dt, "error": str(e), "tb": traceback.format_exc()[-600:]}


def main():
    sample = json.load(open(SAMPLE))["events"]
    roc = {(r["ticker"], r["date"]): r for r in json.load(open(R3 / "roc_values.json"))}
    pe = {(e["ticker"], e["date"]): e for e in json.load(open(R3 / "disabled" / "per_event_summary.json"))}
    traded = {(t["ticker"], t["date"]) for t in json.load(open(R3 / "disabled" / "per_trade.json"))}
    hm = json.load(open(CONFIG_DIR / "hawkes_params.json"))
    qcfg = json.load(open(CONFIG_DIR / "q_bar_tiers.json"))
    pep = {}
    pa = BACKTEST.parent / "results" / "phase_a" / "production_fit_results.json"
    if pa.exists():
        for r in json.load(open(pa)):
            if r.get("status") == "success" and "final_params" in r:
                pep[(r["ticker"], r["date"])] = r["final_params"]
    rho = hm.get("rho", 0.99)
    excl_keys = {(e["ticker"], e["date"]) for e in EXCLUDED}

    tasks = []
    for e in sample:
        k = (e["ticker"], e["date"])
        if k in excl_keys or k not in pe:
            continue
        tasks.append({"ticker": e["ticker"], "date": e["date"], "mom_pct": e["mom_pct"],
                      "scanner_hit_ts_ns": e["scanner_hit_ts_ns"], "stratum": e["stratum"],
                      "gap_pct_at_hit": e["gap_pct_at_hit"],
                      "is_first": bool(roc.get(k, {}).get("is_first_appearance")),
                      "gate_runner": pe[k].get("gate_at_scanner_hit"),
                      "fp": pep.get(k, hm), "rho": rho, "q_bar_cfg": qcfg, "traded": traded})

    print(f"auditing {len(tasks)} processed events (6 workers)...")
    audit = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(audit_one, t): t for t in tasks}
        for fu in as_completed(futs):
            audit.append(fu.result())
    errs = [a for a in audit if "error" in a]
    if errs:
        print(f"AUDIT ERRORS ({len(errs)}): {[(a['ticker'],a['date'],a['error']) for a in errs]}")
    audit = [a for a in audit if "error" not in a]
    audit.sort(key=lambda a: (a["stratum"], a["entry_failure_reason"], a["ticker"]))

    # consistency check (escalation)
    inconsistent = [a for a in audit if not a["consistent"]]
    print(f"CONSISTENCY: {len(inconsistent)} of {len(audit)} disagree with R3 trade/no-trade")
    if inconsistent:
        for a in inconsistent:
            print(f"  !! {a['ticker']} {a['date']}: audit_would_trade={a['audit_would_trade']} r3_traded={a['r3_traded']} reason={a['entry_failure_reason']}")

    (OUT / "entry_audit.json").write_text(json.dumps(audit, indent=2))
    (OUT / "entry_audit_excluded.json").write_text(json.dumps(EXCLUDED, indent=2))

    # T1a: first-appearance with n_before>100
    fa_high = [(a["ticker"], a["date"], a["n_trades_before_scanner"]) for a in audit
               if a["is_first_appearance"] and a["n_trades_before_scanner"] > 100]
    print(f"T1a: first-appearance events with n_before>100: {len(fa_high)} {fa_high}")

    # T1b: failure reason distribution
    dist = Counter(a["entry_failure_reason"] for a in audit)
    print("T1b failure reason distribution (of %d):" % len(audit))
    for r, c in dist.most_common():
        print(f"  {r}: {c} ({100*c/len(audit):.1f}%)")

    # T2 cross-tab
    REASONS = ["TRADED", "ANCHOR_NEVER_FIRED", "ANCHOR_LATE", "WARMUP_AT_DEADLINE",
               "NEVER_PASS_IN_WINDOW", "PASS_TOO_LATE"]
    xtab = {s: {r: 0 for r in REASONS} for s in ("low", "mid", "high")}
    fa_tab = {r: 0 for r in REASONS}
    for a in audit:
        xtab[a["stratum"]][a["entry_failure_reason"]] += 1
        if a["is_first_appearance"]:
            fa_tab[a["entry_failure_reason"]] += 1
    (OUT / "failure_breakdown.json").write_text(json.dumps(
        {"by_stratum": xtab, "first_appearance": fa_tab, "reasons": REASONS}, indent=2))
    print("\nT2 cross-tab (stratum x reason):")
    hdr = "        " + " ".join(f"{r[:10]:>11}" for r in REASONS)
    print(hdr)
    for s in ("low", "mid", "high"):
        print(f"  {s:<5} " + " ".join(f"{xtab[s][r]:>11}" for r in REASONS))
    print(f"  {'1st':<5} " + " ".join(f"{fa_tab[r]:>11}" for r in REASONS))

    # T3 prescan history
    by_reason = defaultdict(list)
    for a in audit:
        by_reason[a["entry_failure_reason"]].append(a)
    presc = {}
    for r in REASONS:
        grp = by_reason.get(r, [])
        if not grp:
            continue
        presc[r] = {
            "n": len(grp),
            "mean_n_trades_before_scanner": round(float(np.mean([g["n_trades_before_scanner"] for g in grp])), 1),
            "mean_lambda_ref": round(float(np.mean([g["lambda_ref_cold_start"] for g in grp])), 6),
            "mean_t_event_relative_sec": round(float(np.mean([g["t_event_anchor_relative_sec"] for g in grp if g["t_event_anchor_relative_sec"] is not None])), 1) if any(g["t_event_anchor_relative_sec"] is not None for g in grp) else None,
        }
    fa = [a for a in audit if a["is_first_appearance"]]
    presc["_first_appearance_all"] = {
        "n": len(fa),
        "mean_n_trades_before_scanner": round(float(np.mean([g["n_trades_before_scanner"] for g in fa])), 1) if fa else None,
        "mean_lambda_ref": round(float(np.mean([g["lambda_ref_cold_start"] for g in fa])), 6) if fa else None,
    }
    (OUT / "prescan_history.json").write_text(json.dumps(presc, indent=2))
    print("\nT3 prescan history by category:")
    print(f"  {'Category':<22} {'N':>4} {'mean_n_pre':>11} {'mean_lref':>11} {'mean_t_ev_rel':>13}")
    for r in REASONS + ["_first_appearance_all"]:
        if r in presc:
            p = presc[r]
            print(f"  {r:<22} {p['n']:>4} {p['mean_n_trades_before_scanner']:>11} "
                  f"{p['mean_lambda_ref']:>11} {str(p.get('mean_t_event_relative_sec')):>13}")

    # T4 selection (<=20): 2-3 per populated failure cat, 3 TRADED controls, 3 first-appearance non-traded
    def pick_representative(grp, n):
        if not grp:
            return []
        med = np.median([g["n_trades_before_scanner"] for g in grp])
        return sorted(grp, key=lambda g: abs(g["n_trades_before_scanner"] - med))[:n]
    selection = []
    for r in REASONS:
        if r == "TRADED":
            continue
        selection += [{**g, "chart_category": r} for g in pick_representative(by_reason.get(r, []), 3)]
    traded_grp = by_reason.get("TRADED", [])
    if traded_grp:
        ts = sorted(traded_grp, key=lambda g: g["n_trades_before_scanner"])
        picks = []
        if ts:
            picks.append(ts[0]); picks.append(ts[-1])
        midgap = [g for g in traded_grp if g["stratum"] in ("mid", "high")]
        if midgap:
            picks.append(midgap[0])
        seen = set()
        for g in picks:
            k = (g["ticker"], g["date"])
            if k not in seen:
                seen.add(k); selection.append({**g, "chart_category": "TRADED_CONTROL"})
    fa_nt = [a for a in audit if a["is_first_appearance"] and not a["r3_traded"]]
    for g in pick_representative(fa_nt, 3):
        if (g["ticker"], g["date"]) not in {(s["ticker"], s["date"]) for s in selection}:
            selection.append({**g, "chart_category": "FIRST_APPEARANCE"})
    # cap 20
    selection = selection[:20]
    sel_out = [{"ticker": s["ticker"], "date": s["date"], "stratum": s["stratum"],
                "failure_reason": s["entry_failure_reason"], "is_first_appearance": s["is_first_appearance"],
                "n_trades_before_scanner": s["n_trades_before_scanner"],
                "chart_category": s["chart_category"], "scanner_hit_t_sec": s["scanner_hit_t_sec"]}
               for s in selection]
    (OUT / "chart_selection.json").write_text(json.dumps(sel_out, indent=2))
    print(f"\nT4 selected {len(sel_out)} events for charts: "
          f"{Counter(s['chart_category'] for s in sel_out)}")


if __name__ == "__main__":
    main()
