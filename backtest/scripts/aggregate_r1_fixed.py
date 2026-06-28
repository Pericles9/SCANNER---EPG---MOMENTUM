"""
FIX-T4E-T6B / R1-FIXED — Aggregate the 6 symmetric-sweep configs into the
required output JSONs.

Reads:   backtest/results/phase_r1_fixed/sym_p{50..75}/{run_summary,per_event_summary,per_trade}.json
Writes:  backtest/results/phase_r1_fixed/symmetric_sweep.json
         backtest/results/phase_r1_fixed/exit_breakdown_by_p.json
         backtest/results/phase_r1_fixed/chatter_diagnostic.json

Per-trade chatter: re-entry is disabled (closed_today=True after first entry),
so there is at most one trade per event. Each traded event's
`n_passtofail_transitions` is therefore the per-trade PASS->FAIL count.
NOTE: that counter spans the whole post-scanner window (it is not clipped to
the hold interval), so it counts gate chatter before entry and after exit too.
"""
import json
import statistics
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent.parent
RESULTS = BASE / "backtest" / "results" / "phase_r1_fixed"
P_VALUES = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75]
CHATTER_FLAG_PCT = 20.0  # flag if >20% of trades have >=3 transitions


def load(p):
    tag = f"sym_p{int(p * 100)}"
    d = RESULTS / tag
    with open(d / "run_summary.json") as f:
        summary = json.load(f)
    with open(d / "per_event_summary.json") as f:
        per_event = json.load(f)
    with open(d / "per_trade.json") as f:
        per_trade = json.load(f)
    return summary, per_event, per_trade


def per_trade_chatter(per_event, per_trade):
    """Return list of n_passtofail_transitions, one per trade (join on event)."""
    ev_key = {}
    for ev in per_event:
        if ev.get("status") == "event":
            ev_key[(ev["ticker"], ev["date"], ev["event_idx"])] = (
                ev.get("n_passtofail_transitions", 0)
            )
    out = []
    for t in per_trade:
        key = (t["ticker"], t["date"], t["event_idx"])
        out.append(ev_key.get(key, 0))
    return out


def main():
    sweep_rows = []
    exit_rows = {}
    chatter_rows = []

    for p in P_VALUES:
        summary, per_event, per_trade = load(p)
        chatter = per_trade_chatter(per_event, per_trade)
        n = len(chatter)
        mean_ptf_per_trade = round(sum(chatter) / n, 3) if n else None

        sweep_rows.append({
            "p": p,
            "n_trades": summary.get("n_trades"),
            "profit_factor": summary.get("profit_factor"),
            "win_rate_pct": summary.get("win_rate"),
            "mean_pnl_pct": summary.get("mean_pnl_pct"),
            "cvar5_pct": summary.get("cvar5_pct"),
            "mean_hold_sec": summary.get("mean_hold_sec"),
            "mean_entry_lag_sec": summary.get("mean_entry_lag_from_scanner_sec"),
            "mean_passtofail_per_trade": mean_ptf_per_trade,
            "exit_reason_breakdown": summary.get("exit_reason_breakdown", {}),
        })

        exit_rows[f"p{int(p * 100)}"] = {
            "p": p,
            "n_trades": summary.get("n_trades"),
            "exit_reason_breakdown": summary.get("exit_reason_breakdown", {}),
        }

        n_ge3 = sum(1 for c in chatter if c >= 3)
        pct_ge3 = round(100 * n_ge3 / n, 1) if n else None
        chatter_rows.append({
            "p": p,
            "n_trades": n,
            "mean_passtofail_per_trade": mean_ptf_per_trade,
            "median_passtofail_per_trade": (
                round(float(statistics.median(chatter)), 3) if n else None
            ),
            "p90_passtofail_per_trade": (
                round(float(_p90(chatter)), 3) if n else None
            ),
            "max_passtofail_per_trade": max(chatter) if n else None,
            "n_trades_ge3_transitions": n_ge3,
            "pct_trades_ge3_transitions": pct_ge3,
            "flag_gt20pct_ge3": bool(pct_ge3 is not None and pct_ge3 > CHATTER_FLAG_PCT),
            "transition_distribution": _dist(chatter),
        })

    with open(RESULTS / "symmetric_sweep.json", "w") as f:
        json.dump(sweep_rows, f, indent=2)
    with open(RESULTS / "exit_breakdown_by_p.json", "w") as f:
        json.dump(exit_rows, f, indent=2)
    with open(RESULTS / "chatter_diagnostic.json", "w") as f:
        json.dump(chatter_rows, f, indent=2)

    print("Wrote symmetric_sweep.json, exit_breakdown_by_p.json, chatter_diagnostic.json")
    for r in sweep_rows:
        eb = r["exit_reason_breakdown"]
        epg_pct = eb.get("epg_window_close", {}).get("pct_of_trades", 0.0)
        se_pct = eb.get("session_end", {}).get("pct_of_trades", 0.0)
        print(
            f"  p={r['p']:.2f}  n={r['n_trades']}  PF={r['profit_factor']}  "
            f"epg_close%={epg_pct}  session_end%={se_pct}  "
            f"hold={r['mean_hold_sec']}  CVaR5={r['cvar5_pct']}  "
            f"ptf/trade={r['mean_passtofail_per_trade']}"
        )


def _p90(xs):
    s = sorted(xs)
    if len(s) == 1:
        return s[0]
    # linear interpolation, same convention as numpy default
    import numpy as np
    return np.percentile(s, 90)


def _dist(xs):
    from collections import Counter
    return dict(sorted(Counter(xs).items()))


if __name__ == "__main__":
    main()
