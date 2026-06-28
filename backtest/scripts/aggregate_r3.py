"""
Phase R3 T3/T4/T5 — derive the ROC sweep by post-filtering the disabled-arm run.

ROC is a pure event ADMIT/BLOCK gate applied before entry; it does not alter the
within-event entry/exit of admitted events (and runner_rapid does not implement it —
AUDIT-RAPID T8). So each arm = the disabled-arm result restricted to admitted events:
  admitted  iff  is_first_appearance  OR  roc_5m >= roc_min
  blocked   iff  (not first_appearance) and roc_5m < roc_min
Partial-window events carry a (partial) roc value and are threshold-checked with it.

Reads:  phase_r3/disabled/{per_trade,per_event_summary,run_summary}.json
        phase_r3/roc_values.json ; data/val_r3_stratified.json
Writes: phase_r3/roc_sweep.json, selection_value.json, partial_window.json
"""
from __future__ import annotations
import json
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
R3 = BACKTEST / "results" / "phase_r3"
DIS = R3 / "disabled"
ARMS = [0.05, 0.10, 0.15, 0.20, 0.25]
ACTUAL_PF_NOTE = "disabled-arm PF on val_r3"


def cvar5(pnl):
    s = np.sort(np.asarray(pnl, dtype=float)); n = len(s)
    if n == 0:
        return None
    return round(float(np.mean(s[:max(1, int(0.05 * n))])), 4)


def pf(pnl):
    a = np.asarray(pnl, dtype=float)
    w = float(a[a > 0].sum()); l = float(abs(a[a < 0].sum()))
    return round(w / l, 4) if l > 0 else None


def exit_breakdown(trades):
    out = {}
    n = len(trades)
    from collections import Counter
    c = Counter(t["exit_reason"] for t in trades)
    for r, cnt in c.items():
        out[r] = {"count": cnt, "pct_of_trades": round(100 * cnt / n, 2) if n else 0.0}
    return out


def agg_metrics(trades):
    if not trades:
        return dict(n_traded=0, profit_factor=None, win_rate_pct=None, mean_pnl_pct=None,
                    cvar5_pct=None, mean_hold_sec=None, mean_entry_lag_sec=None,
                    exit_breakdown={})
    pnl = [t["pnl_pct"] for t in trades]
    lags = [t["entry_lag_from_scanner_sec"] for t in trades
            if t.get("entry_lag_from_scanner_sec") is not None]
    return dict(
        n_traded=len(trades),
        profit_factor=pf(pnl),
        win_rate_pct=round(100 * np.mean([p > 0 for p in pnl]), 2),
        mean_pnl_pct=round(float(np.mean(pnl)), 4),
        cvar5_pct=cvar5(pnl),
        mean_hold_sec=round(float(np.mean([t["hold_sec"] for t in trades])), 2),
        mean_entry_lag_sec=round(float(np.mean(lags)), 2) if lags else None,
        exit_breakdown=exit_breakdown(trades),
    )


def main():
    per_trade = json.load(open(DIS / "per_trade.json"))
    roc = {(r["ticker"], r["date"]): r for r in json.load(open(R3 / "roc_values.json"))}
    sample = json.load(open(BACKTEST / "data" / "val_r3_stratified.json"))["events"]
    all_keys = [(e["ticker"], e["date"]) for e in sample]
    trade_by = {(t["ticker"], t["date"]): t for t in per_trade}

    def admitted(key, rmin):
        r = roc[key]
        if r.get("error"):
            return True  # no roc computable -> treat as first-appearance admit
        if r["is_first_appearance"]:
            return True
        return r["scanner_roc_5m_at_fire"] >= rmin

    n_first = sum(1 for k in all_keys if roc[k].get("is_first_appearance"))

    # ── T3 sweep ──
    sweep = []
    # disabled row
    dis_trades = list(per_trade)
    sweep.append({"roc_min": "disabled", "n_events_attempted": len(all_keys),
                  "n_blocked_by_roc": 0, "n_first_appearance_skip": n_first,
                  "n_partial_window": sum(1 for k in all_keys if roc[k].get("is_partial_window")),
                  **agg_metrics(dis_trades)})
    for rmin in ARMS:
        adm = [k for k in all_keys if admitted(k, rmin)]
        blk = [k for k in all_keys if not admitted(k, rmin)]
        adm_trades = [trade_by[k] for k in adm if k in trade_by]
        sweep.append({
            "roc_min": rmin,
            "n_events_attempted": len(adm),
            "n_blocked_by_roc": len(blk),
            "n_first_appearance_skip": n_first,
            "n_partial_window": sum(1 for k in adm if roc[k].get("is_partial_window")),
            **agg_metrics(adm_trades),
        })
    (R3 / "roc_sweep.json").write_text(json.dumps(sweep, indent=2))

    # ── T4 selection value ──
    sel = []
    for rmin in ARMS:
        adm = [k for k in all_keys if admitted(k, rmin)]
        blk = [k for k in all_keys if not admitted(k, rmin)]
        adm_tr = [trade_by[k] for k in adm if k in trade_by]
        blk_tr = [trade_by[k] for k in blk if k in trade_by]
        a_pnl = [t["pnl_pct"] for t in adm_tr]; b_pnl = [t["pnl_pct"] for t in blk_tr]
        a_pf, b_pf = pf(a_pnl), pf(b_pnl)
        sel.append({
            "roc_min": rmin,
            "admitted_pf": a_pf, "admitted_cvar5": cvar5(a_pnl), "admitted_n": len(adm_tr),
            "blocked_pf": b_pf, "blocked_cvar5": cvar5(b_pnl), "blocked_n": len(blk_tr),
            "blocked_unreliable": len(blk_tr) < 5,
            "delta_pf": (round(a_pf - b_pf, 4) if (a_pf is not None and b_pf is not None) else None),
        })
    (R3 / "selection_value.json").write_text(json.dumps(sel, indent=2))

    # ── T5 partial window sensitivity (disabled arm trades) ──
    def grp(pred):
        tr = [trade_by[k] for k in all_keys if k in trade_by and pred(roc[k])]
        return {"n": len(tr), "pf": pf([t["pnl_pct"] for t in tr]),
                "cvar5": cvar5([t["pnl_pct"] for t in tr])}
    full = grp(lambda r: (not r.get("is_first_appearance")) and (not r.get("is_partial_window"))
               and r.get("scanner_roc_window_sec_actual") is not None
               and r["scanner_roc_window_sec_actual"] >= 300.0)
    partial = grp(lambda r: r.get("is_partial_window"))
    first = grp(lambda r: r.get("is_first_appearance"))
    pw = {"full_window": full, "partial_window": partial, "first_appearance": first,
          "note": "full = roc window>=300s; partial = is_partial_window (window<300s); "
                  "first_appearance reported separately (no roc, always admitted)."}
    (R3 / "partial_window.json").write_text(json.dumps(pw, indent=2))

    # ── console report ──
    print("=== T3 ROC sweep ===")
    for r in sweep:
        eb = r["exit_breakdown"]
        tg = eb.get("time_gate", {}).get("pct_of_trades", 0.0)
        ec = eb.get("epg_window_close", {}).get("pct_of_trades", 0.0)
        print(f"  roc_min={str(r['roc_min']):>8}  attempt={r['n_events_attempted']:>3} "
              f"blocked={r['n_blocked_by_roc']:>3} 1st={r['n_first_appearance_skip']:>2} "
              f"traded={r['n_traded']:>3}  PF={r['profit_factor']}  CVaR5={r['cvar5_pct']} "
              f"hold={r['mean_hold_sec']}  tg%={tg} epg%={ec}")
    print("=== T4 selection value ===")
    for r in sel:
        print(f"  roc_min={r['roc_min']:.2f}  admPF={r['admitted_pf']} admCVaR5={r['admitted_cvar5']} admN={r['admitted_n']} | "
              f"blkPF={r['blocked_pf']} blkCVaR5={r['blocked_cvar5']} blkN={r['blocked_n']}"
              f"{' (UNRELIABLE n<5)' if r['blocked_unreliable'] else ''}  dPF={r['delta_pf']}")
    print("=== T5 partial window ===")
    print(f"  full:  {full}")
    print(f"  part:  {partial}")
    print(f"  first: {first}")
    print("wrote roc_sweep.json, selection_value.json, partial_window.json")


if __name__ == "__main__":
    main()
