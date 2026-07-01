"""
Phase REBUILD-VAL T5 — DIAG-ENTRY re-run on val_r4_stratified.json.

Entry-failure classification for all 100 events in the r4 sample. Replays
the full Hawkes/gate pipeline per event (same logic as runner_rapid) and
classifies the outcome:

  TRADED              — event entered in T4 p=0.65 baseline run
  ANCHOR_NEVER_FIRED  — anchor threshold never crossed
  ANCHOR_LATE         — anchor fired after the T_gate window
  WARMUP_AT_DEADLINE  — anchor fired but warmup ends after deadline
  NEVER_PASS_IN_WINDOW — anchor fired, warmup OK, but gate never passed
  PASS_TOO_LATE       — first PASS was after the T_gate deadline

T_gate: 500s (option A, max_entry_lag_sec=500, confirmed 2026-06-30)
Reference run: phase_r1_final/sym_p65 (p_open=p_close=0.65)

Writes: phase_diag_entry_r4/entry_audit.json, summary.md
"""
from __future__ import annotations
import json
import math
import sys
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))
sys.path.insert(0, str(BACKTEST.parent))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event
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

OUT    = BACKTEST / "results" / "phase_diag_entry_r4"
SAMPLE = BACKTEST / "data" / "val_r4_stratified.json"
R1_P65 = BACKTEST / "results" / "phase_r1_final" / "sym_p65"

OUT.mkdir(parents=True, exist_ok=True)

P_OPEN = P_CLOSE = 0.65
TAU_PEAK = 600.0
GATE_C   = 1.5
GATE_MODE = "peak"
MAX_LAG  = 500.0   # T_gate option A


def audit_one(task):
    tk, dt = task["ticker"], task["date"]
    try:
        td = load_trades(tk, dt, task["mom_pct"])
        qd = load_quotes(tk, dt, task["mom_pct"])
        pc = get_prev_close(tk, dt)
        if td is None or qd is None:
            return {"ticker": tk, "date": dt, "error": "load_failed"}

        N = td.n_trades
        t_sec = np.asarray(td.t_sec, dtype=float)
        scanner_t = (int(task["scanner_hit_ts_ns"]) - int(td.timestamps[0])) / NS_PER_SECOND
        deadline  = scanner_t + MAX_LAG

        fp  = task["fp"]
        rho = task["rho"]
        tier = task["q_bar_cfg"].get("wide", {}).get("median", 250.0)

        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier,
        )
        sides  = ofi.sides
        halts  = _build_halt_intervals(td)
        lb = np.zeros(N); ls = np.zeros(N); E = np.zeros(N)
        Ed = np.zeros(N); nb = np.zeros(N)
        dv = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
        glref = fp["mu_buy"] + fp["mu_sell"]
        plref = compute_lambda_ref_per_event(tk, dt)
        lref  = glref if (math.isnan(plref) or plref <= 0) else plref

        cold = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=rho, lambda_ref=lref,
            init_params=fp, rho_E=rho,
            lam_buy_out=lb, lam_sell_out=ls, E_out=E, Edot_out=Ed,
            n_base_out=nb, dv_arr=dv, halt_intervals=halts or None,
        )
        lambda_hat = lb + ls

        anchor = EventAnchor(lambda_ref=glref, k_multiplier=EPG_K)
        if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
            anchor.set_lambda_ref(cold.mu_buy + cold.mu_sell)
        gate = ParticipationGate(
            half_life_seconds=EPG_TAU, peak_threshold_p=P_OPEN,
            warmup_seconds=EPG_WARMUP, gate_mode=GATE_MODE,
            tau_peak=TAU_PEAK, C=GATE_C, p_open=P_OPEN, p_close=P_CLOSE,
        )
        t_event = None
        states  = []
        for i in range(N):
            ev = anchor.update(lambda_hat[i], t_sec[i])
            if ev is not None and t_event is None:
                t_event = float(t_sec[i])
                gate.activate(ev)
            gt = t_sec[i]
            if i > 0 and halts and t_sec[i] - t_sec[i - 1] > HALT_GAP_THRESHOLD:
                for hs, he in halts:
                    if t_sec[i - 1] < he and t_sec[i] > hs:
                        gt = t_sec[i - 1] + 1e-6
                        break
            states.append(gate.update(float(td.prices[i]) * float(td.sizes[i]), gt).name)

        states = np.array(states)
        n_before     = int(np.sum(t_sec < scanner_t))
        pass_all     = states == "PASS"
        first_pass_overall = float(t_sec[pass_all][0]) if pass_all.any() else None
        win          = (t_sec >= scanner_t) & (t_sec <= deadline)
        pass_win     = win & (states == "PASS")
        any_pass     = bool(pass_win.any())
        t_first_pass = float(t_sec[pass_win][0]) if any_pass else None

        anchor_fired = t_event is not None
        t_ev_rel     = (t_event - scanner_t) if anchor_fired else None
        t_warm_end   = (t_event + EPG_WARMUP)  if anchor_fired else None
        warm_after   = (t_warm_end > deadline)  if anchor_fired else None

        r1_traded = (tk, dt) in task["traded"]

        if r1_traded:
            reason = "TRADED"
        elif not anchor_fired:
            reason = "ANCHOR_NEVER_FIRED"
        elif t_ev_rel > MAX_LAG:
            reason = "ANCHOR_LATE"
        elif warm_after:
            reason = "WARMUP_AT_DEADLINE"
        elif first_pass_overall is None:
            reason = "NEVER_PASS_IN_WINDOW"
        else:
            reason = "PASS_TOO_LATE"

        return {
            "ticker": tk,
            "date":   dt,
            "stratum": task["stratum"],
            "mom_pct": task["mom_pct"],
            "gap_pct_at_hit": task["gap_pct_at_hit"],
            "prev_close": round(float(pc), 4) if pc is not None else None,
            "sub_dollar": bool(pc is not None and pc < 1.0),
            "n_trades_before_scanner": n_before,
            "n_ticks_session_total": N,
            "anchor_fired": anchor_fired,
            "t_event_anchor_relative_sec": round(t_ev_rel, 3) if anchor_fired else None,
            "warmup_ends_after_deadline": warm_after,
            "any_pass_in_entry_window": any_pass,
            "t_first_pass_relative_sec": round(t_first_pass - scanner_t, 3) if t_first_pass is not None else None,
            "entry_failure_reason": reason,
            "r1_traded": r1_traded,
            "consistent": (any_pass == r1_traded),
        }
    except Exception as e:
        import traceback
        return {"ticker": tk, "date": dt, "error": str(e), "tb": traceback.format_exc()[-600:]}


def main():
    sample = json.load(open(SAMPLE))["events"]
    hm     = json.load(open(CONFIG_DIR / "hawkes_params.json"))
    qcfg   = json.load(open(CONFIG_DIR / "q_bar_tiers.json"))
    rho    = hm.get("rho", 0.99)

    # Load T4 p=0.65 traded set (reference for TRADED classification)
    traded: set[tuple[str, str]] = set()
    pt_path = R1_P65 / "per_trade.json"
    if pt_path.exists():
        for t in json.load(open(pt_path)):
            traded.add((t["ticker"], t["date"]))
        print(f"Loaded T4 p=0.65 traded set: {len(traded)} trades")
    else:
        print(f"WARNING: T4 per_trade.json not found at {pt_path} — TRADED will be empty")

    tasks = []
    for e in sample:
        tasks.append({
            "ticker": e["ticker"],
            "date":   e["date"],
            "mom_pct": e["mom_pct"],
            "scanner_hit_ts_ns": e["scanner_hit_ts_ns"],
            "stratum": e["stratum"],
            "gap_pct_at_hit": e["gap_pct_at_hit"],
            "fp": hm,
            "rho": rho,
            "q_bar_cfg": qcfg,
            "traded": traded,
        })

    print(f"Auditing {len(tasks)} events (6 workers, max_lag={MAX_LAG}s)...")
    audit = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(audit_one, t): t for t in tasks}
        for fu in as_completed(futs):
            audit.append(fu.result())

    errs = [a for a in audit if "error" in a]
    if errs:
        print(f"ERRORS ({len(errs)}): {[(a['ticker'], a['date'], a['error']) for a in errs]}")
    audit = [a for a in audit if "error" not in a]
    audit.sort(key=lambda a: (a["stratum"], a["entry_failure_reason"], a["ticker"]))

    inconsistent = [a for a in audit if not a["consistent"]]
    print(f"Consistency: {len(inconsistent)} of {len(audit)} disagree with r1-p65 traded set")
    if inconsistent:
        for a in inconsistent:
            print(f"  !! {a['ticker']} {a['date']}: any_pass={a['any_pass_in_entry_window']} r1_traded={a['r1_traded']} reason={a['entry_failure_reason']}")

    (OUT / "entry_audit.json").write_text(json.dumps(audit, indent=2))

    # ── Summary ────────────────────────────────────────────────────────
    REASONS = ["TRADED", "ANCHOR_NEVER_FIRED", "ANCHOR_LATE", "WARMUP_AT_DEADLINE",
               "NEVER_PASS_IN_WINDOW", "PASS_TOO_LATE"]
    STRATA  = ("low", "mid", "high")

    xtab = {s: {r: 0 for r in REASONS} for s in STRATA}
    for a in audit:
        xtab[a["stratum"]][a["entry_failure_reason"]] += 1

    # Sub-$1 fraction per stratum
    sub1 = {s: {"n_sub1": 0, "n_total": 0} for s in STRATA}
    for a in audit:
        st = a["stratum"]
        sub1[st]["n_total"] += 1
        if a.get("sub_dollar"):
            sub1[st]["n_sub1"] += 1

    # Failure reason distribution overall
    dist = Counter(a["entry_failure_reason"] for a in audit)

    print("\nEntry failure reason distribution:")
    for r, c in dist.most_common():
        print(f"  {r}: {c} ({100*c/len(audit):.1f}%)")

    print("\nFailure reason × stratum:")
    hdr = "        " + " ".join(f"{r[:10]:>11}" for r in REASONS)
    print(hdr)
    for s in STRATA:
        sub1_pct = 100 * sub1[s]["n_sub1"] / max(sub1[s]["n_total"], 1)
        print(f"  {s:<5} " + " ".join(f"{xtab[s][r]:>11}" for r in REASONS)
              + f"   sub-$1: {sub1[s]['n_sub1']}/{sub1[s]['n_total']} ({sub1_pct:.0f}%)")

    # ── Write summary.md ───────────────────────────────────────────────
    rows_md = []
    for a in audit:
        rows_md.append(
            f"| {a['ticker']} | {a['date']} | {a['stratum']} "
            f"| {a['mom_pct']:.2f} | {a['gap_pct_at_hit']:.2f} "
            f"| {a['prev_close'] if a['prev_close'] is not None else 'N/A'} "
            f"| {a['n_trades_before_scanner']} | {a['entry_failure_reason']} |"
        )

    xtab_md = "| Stratum | " + " | ".join(REASONS) + " | sub-$1 frac |\n"
    xtab_md += "|---------|" + "|".join(["---"] * (len(REASONS) + 1)) + "|\n"
    for s in STRATA:
        frac = f"{sub1[s]['n_sub1']}/{sub1[s]['n_total']}"
        xtab_md += f"| {s} | " + " | ".join(str(xtab[s][r]) for r in REASONS) + f" | {frac} |\n"

    md = f"""---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-06-30
phase: REBUILD-VAL T5 (DIAG-ENTRY r4)
---

# Phase DIAG-ENTRY r4 — Entry Failure Classification on val_r4_stratified

Sample: `val_r4_stratified.json` (n=100, mom_pct tercile strata 30/40/30)
T_gate: max_entry_lag_sec=500s (option A)
Reference run: phase_r1_final/sym_p65 (p_open=p_close=0.65)

## Failure Reason × Stratum

{xtab_md}

## Per-Event Detail

| Ticker | Date | Stratum | mom_pct | gap_pct_at_hit | prev_close | n_trades_pre | entry_failure_reason |
|--------|------|---------|---------|----------------|------------|--------------|---------------------|
{"".join(rows_md)}

## Escalation Check

- n_audit = {len(audit)}
- errors  = {len(errs)}
- inconsistent with r1-p65 runner = {len(inconsistent)}
"""
    (OUT / "summary.md").write_text(md, encoding="utf-8")
    print(f"\nWrote {OUT / 'entry_audit.json'} and {OUT / 'summary.md'}")
    print(f"Total audited: {len(audit)}")


if __name__ == "__main__":
    main()
