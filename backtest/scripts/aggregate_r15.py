"""
Phase R1.5 — aggregate the time-gate sweep arms and join to the R1-Fixed p=0.65 baseline.

Reads:  phase_r15/t_gate_{400,500,600}/{run_summary,per_trade}.json
        phase_r1_fixed/sym_p65/{run_summary,per_trade}.json   (baseline, read-only)
Writes: phase_r15/sweep_results.json
        phase_r15/per_trade/t_gate_{N}.json
        phase_r15/false_gate_analysis.json
"""
import json
from pathlib import Path

import numpy as np

BASE = Path(__file__).resolve().parent.parent.parent
R15 = BASE / "backtest" / "results" / "phase_r15"
BASELINE = BASE / "backtest" / "results" / "phase_r1_fixed" / "sym_p65"
T_GATES = [400, 500, 600]
ACTUAL_PF = 2.6584
ACTUAL_CVAR5 = -27.74


def cvar5(pnl):
    s = np.sort(np.asarray(pnl, dtype=float))
    n = len(s)
    if n == 0:
        return None
    k = max(1, int(0.05 * n))
    return round(float(np.mean(s[:k])), 4)


def pf(pnl):
    a = np.asarray(pnl, dtype=float)
    w = float(a[a > 0].sum()); l = float(abs(a[a < 0].sum()))
    return round(w / l, 4) if l > 0 else None


def main():
    with open(BASELINE / "per_trade.json") as f:
        base_trades = json.load(f)
    base = {(t["ticker"], t["date"]): t for t in base_trades}
    with open(BASELINE / "run_summary.json") as f:
        base_sum = json.load(f)

    (R15 / "per_trade").mkdir(parents=True, exist_ok=True)

    sweep_rows = [{
        "t_gate_sec": None,
        "n_trades": base_sum["n_trades"],
        "n_open_at_tgate": None,
        "n_time_gate_exits": 0, "pct_time_gate": 0.0,
        "n_epg_close_exits": base_sum["exit_reason_breakdown"].get("epg_window_close", {}).get("count", 0),
        "pct_epg_close": base_sum["exit_reason_breakdown"].get("epg_window_close", {}).get("pct_of_trades", 0.0),
        "n_session_end_exits": base_sum["exit_reason_breakdown"].get("session_end", {}).get("count", 0),
        "pct_session_end": base_sum["exit_reason_breakdown"].get("session_end", {}).get("pct_of_trades", 0.0),
        "profit_factor": base_sum["profit_factor"],
        "win_rate_pct": base_sum["win_rate"],
        "mean_pnl_pct": base_sum["mean_pnl_pct"],
        "cvar5_pct": base_sum["cvar5_pct"],
        "mean_hold_sec": base_sum["mean_hold_sec"],
        "mean_entry_lag_sec": base_sum.get("mean_entry_lag_from_scanner_sec"),
        "false_gate_rate": None, "true_gate_rate": None,
    }]

    false_gate_analysis = {}

    for tg in T_GATES:
        d = R15 / f"t_gate_{tg}"
        with open(d / "run_summary.json") as f:
            s = json.load(f)
        with open(d / "per_trade.json") as f:
            trades = json.load(f)
        eb = s.get("exit_reason_breakdown", {})
        n = len(trades)

        n_open_at_tgate = sum(1 for t in base_trades if t["hold_sec"] > tg)

        tg_exits = [t for t in trades if t["exit_reason"] == "time_gate"]
        n_false = n_true = 0
        fa_rows = []
        for t in tg_exits:
            b = base.get((t["ticker"], t["date"]))
            no_gate_pnl = b["pnl_pct"] if b else None
            is_false = (no_gate_pnl is not None and no_gate_pnl > 0)
            if is_false:
                n_false += 1
            else:
                n_true += 1
            fa_rows.append({
                "ticker": t["ticker"], "date": t["date"],
                "no_gate_pnl_pct": no_gate_pnl,
                "no_gate_exit_reason": (b["exit_reason"] if b else None),
                "no_gate_hold_sec": (b["hold_sec"] if b else None),
                "gated_pnl_pct": t["pnl_pct"],
                "false_gate": bool(is_false),
            })
        n_tg = len(tg_exits)
        false_gate_analysis[f"t_gate_{tg}"] = {
            "t_gate_sec": tg, "n_time_gate_exits": n_tg,
            "n_false_gate": n_false, "n_true_gate": n_true,
            "false_gate_rate": round(n_false / n_tg, 4) if n_tg else None,
            "true_gate_rate": round(n_true / n_tg, 4) if n_tg else None,
            "trades": fa_rows,
        }

        # per_trade output with derived t_gate_checked + open_pnl_at_gate_check
        out_trades = []
        for t in trades:
            checked = t["hold_sec"] >= tg
            opnl = (t["pnl_pct"] / 100.0) if t["exit_reason"] == "time_gate" else None
            out_trades.append({
                "ticker": t["ticker"], "date": t["date"],
                "exit_reason": t["exit_reason"],
                "entry_t_sec": t["entry_t_sec"], "exit_t_sec": t["exit_t_sec"],
                "hold_sec": t["hold_sec"], "pnl_pct": t["pnl_pct"],
                "t_gate_checked": bool(checked),
                "open_pnl_at_gate_check": opnl,
            })
        with open(R15 / "per_trade" / f"t_gate_{tg}.json", "w") as f:
            json.dump(out_trades, f, indent=2)

        sweep_rows.append({
            "t_gate_sec": tg,
            "n_trades": n,
            "n_open_at_tgate": n_open_at_tgate,
            "n_time_gate_exits": eb.get("time_gate", {}).get("count", 0),
            "pct_time_gate": eb.get("time_gate", {}).get("pct_of_trades", 0.0),
            "n_epg_close_exits": eb.get("epg_window_close", {}).get("count", 0),
            "pct_epg_close": eb.get("epg_window_close", {}).get("pct_of_trades", 0.0),
            "n_session_end_exits": eb.get("session_end", {}).get("count", 0),
            "pct_session_end": eb.get("session_end", {}).get("pct_of_trades", 0.0),
            "profit_factor": s["profit_factor"],
            "win_rate_pct": s["win_rate"],
            "mean_pnl_pct": s["mean_pnl_pct"],
            "cvar5_pct": s["cvar5_pct"],
            "mean_hold_sec": s["mean_hold_sec"],
            "mean_entry_lag_sec": s.get("mean_entry_lag_from_scanner_sec"),
            "false_gate_rate": false_gate_analysis[f"t_gate_{tg}"]["false_gate_rate"],
            "true_gate_rate": false_gate_analysis[f"t_gate_{tg}"]["true_gate_rate"],
        })

    with open(R15 / "sweep_results.json", "w") as f:
        json.dump(sweep_rows, f, indent=2)
    with open(R15 / "false_gate_analysis.json", "w") as f:
        json.dump(false_gate_analysis, f, indent=2)

    print("Wrote sweep_results.json, per_trade/t_gate_*.json, false_gate_analysis.json")
    for r in sweep_rows:
        tg = r["t_gate_sec"]
        print(f"  T_gate={str(tg):>4}  n={r['n_trades']}  open@check={r['n_open_at_tgate']}  "
              f"tg={r['n_time_gate_exits']} epg={r['n_epg_close_exits']} se={r['n_session_end_exits']}  "
              f"PF={r['profit_factor']}  CVaR5={r['cvar5_pct']}  hold={r['mean_hold_sec']}  "
              f"false={r['false_gate_rate']} true={r['true_gate_rate']}")


if __name__ == "__main__":
    main()
