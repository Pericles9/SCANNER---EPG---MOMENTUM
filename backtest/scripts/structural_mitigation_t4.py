#!/usr/bin/env python3
"""
Structural Mitigation — T4 Test B: staged entry (reads T1 only).

Half position at gate PASS (entry). Second half added iff the gate is still PASS at
T=60s since entry — a fixed, pre-stated checkpoint (round number, NOT optimized). In the
no-gate baseline the gate is PASS for the whole hold, so "still PASS at 60s" == hold>=60s.

Normalization: 1 unit of intended capital per trade.
  confirmed  (hold>=60): 0.5 @ entry_price + 0.5 @ price_at_60s  -> blended return
  unconfirmed(hold<60) : 0.5 @ entry_price only (stays half size) -> 0.5 * baseline return
"""
from __future__ import annotations

import json
import numpy as np
import plotly.graph_objects as go

import tail_risk_lib as L
from structural_mitigation_t3 import metrics, _comparison_bar

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
CHARTS = SM / "charts"


def staged_pnl(r):
    base = r["baseline_pnl_pct"]
    if r["gate_pass_at_60s"] and r["price_at_60s"]:
        leg1 = 0.5 * base
        leg2 = 0.5 * (r["exit_price"] - r["price_at_60s"]) / r["price_at_60s"] * 100.0
        return leg1 + leg2, True
    return 0.5 * base, False


def main():
    CHARTS.mkdir(parents=True, exist_ok=True)
    data = json.load(open(SM / "raw_trajectories.json"))
    tr = data["trades"]
    tg300 = data["meta"]["tgate300_ref"]

    for r in tr:
        r["staged_pnl"], r["confirmed"] = staged_pnl(r)

    base_pnls = [r["baseline_pnl_pct"] for r in tr]
    staged_pnls = [r["staged_pnl"] for r in tr]
    m_base = metrics(base_pnls)
    m_stag = metrics(staged_pnls)

    W = [r for r in tr if r["is_winner"]]
    Lz = [r for r in tr if not r["is_winner"]]
    win_conf = sum(r["confirmed"] for r in W)
    los_conf = sum(r["confirmed"] for r in Lz)
    win_conf_rate = 100 * win_conf / len(W)
    los_conf_rate = 100 * los_conf / len(Lz)

    confirmed = [r for r in tr if r["confirmed"]]
    unconfirmed = [r for r in tr if not r["confirmed"]]
    mean_conf_staged = float(np.mean([r["staged_pnl"] for r in confirmed])) if confirmed else 0.0
    mean_unconf_staged = float(np.mean([r["staged_pnl"] for r in unconfirmed])) if unconfirmed else 0.0
    mean_unconf_full = float(np.mean([r["baseline_pnl_pct"] for r in unconfirmed])) if unconfirmed else 0.0
    # EV given up on winners that stayed half (unconfirmed winners)
    unconf_winners = [r for r in unconfirmed if r["is_winner"]]
    ev_given_up = float(np.sum([0.5 * r["baseline_pnl_pct"] for r in unconf_winners]))

    # Chart 1 — comparison bar
    _comparison_bar("T4·1 · Baseline vs staged-entry vs T_gate=300",
                    {"Baseline": m_base, "Staged entry": m_stag,
                     "T_gate=300": dict(pf=tg300["pf"], wr=tg300["wr"], mean=tg300["mean_pnl"], cvar5=tg300["cvar5"])},
                    CHARTS / "staged_comparison_bar.html")

    # Chart 2 — confirmation rate winners vs losers
    fig = go.Figure()
    fig.add_trace(go.Bar(x=["Eventual winners", "Eventual losers"],
                  y=[win_conf_rate, los_conf_rate],
                  marker_color=["#66BB6A", "#EF5350"],
                  text=[f"{win_conf_rate:.0f}%<br>({win_conf}/{len(W)})",
                        f"{los_conf_rate:.0f}%<br>({los_conf}/{len(Lz)})"],
                  textposition="outside"))
    fig.update_layout(**L.base_layout(
        f"T4·2 · % receiving 2nd tranche (gate PASS at 60s) — winners {win_conf_rate:.0f}% vs losers {los_conf_rate:.0f}%",
        "", "% confirmed"))
    fig.update_yaxes(range=[0, 110])
    fig.write_html(str(CHARTS / "staged_confirm_rate_bar.html"), include_plotlyjs="cdn")
    print("  wrote staged_confirm_rate_bar.html")

    result = dict(
        baseline=m_base, staged=m_stag, tgate300=tg300,
        win_confirm_rate=round(win_conf_rate, 1), los_confirm_rate=round(los_conf_rate, 1),
        win_conf=f"{win_conf}/{len(W)}", los_conf=f"{los_conf}/{len(Lz)}",
        mean_confirmed_staged=round(mean_conf_staged, 3),
        mean_unconfirmed_staged=round(mean_unconf_staged, 3),
        mean_unconfirmed_fullsize=round(mean_unconf_full, 3),
        n_confirmed=len(confirmed), n_unconfirmed=len(unconfirmed),
        n_unconfirmed_winners=len(unconf_winners),
        ev_given_up_on_late_winners_pp=round(ev_given_up, 2),
        cvar5_change_pp=round(m_stag["cvar5"] - m_base["cvar5"], 2),
        mean_change_pp=round(m_stag["mean"] - m_base["mean"], 3),
        useful_work=bool(los_conf_rate < win_conf_rate - 10),
    )
    json.dump(result, open(SM / "t4_result.json", "w"), indent=2)

    verdict = ("Staged entry does useful selective work" if result["useful_work"]
               else "Staged entry does NOT do useful selective work — winners and losers "
                    "confirm at nearly the same rate, so it just uniformly halves exposure")
    md = f"""# T4 — Test B: Staged entry

Half at entry, second half at **T=60s if gate still PASS** (fixed checkpoint, not optimized;
in the no-gate baseline gate PASS throughout ⇒ confirmation ≡ hold≥60s). Reads `raw_trajectories.json`.

**Charts:** [staged_comparison_bar.html](charts/staged_comparison_bar.html) · [staged_confirm_rate_bar.html](charts/staged_confirm_rate_bar.html)

## Metrics (1 unit intended capital per trade)

| config | n | PF | WR% | mean PnL% | CVaR5% |
|---|---|----|----|-----------|--------|
| Baseline (full size) | {m_base['n']} | {m_base['pf']} | {m_base['wr']} | {m_base['mean']} | {m_base['cvar5']} |
| Staged entry | {m_stag['n']} | {m_stag['pf']} | {m_stag['wr']} | {m_stag['mean']} | {m_stag['cvar5']} |
| T_gate=300 (ref) | 65 | {tg300['pf']} | {tg300['wr']} | {tg300['mean_pnl']} | {tg300['cvar5']} |

## T4a — Confirmation rates & EV cost

- Winners receiving 2nd tranche: **{win_conf_rate:.0f}%** ({win_conf}/{len(W)})
- Losers receiving 2nd tranche: **{los_conf_rate:.0f}%** ({los_conf}/{len(Lz)})
- Mean staged PnL: confirmed **{mean_conf_staged:+.2f}%** vs unconfirmed (half-size) **{mean_unconf_staged:+.2f}%**
- Of {len(unconfirmed)} unconfirmed trades, {len(unconf_winners)} were eventual winners left at half size —
  EV given up on them ≈ **{ev_given_up:.1f}pp**.

**Verdict:** {verdict}. CVaR5 {result['cvar5_change_pp']:+.2f}pp, mean {result['mean_change_pp']:+.3f}pp vs baseline.
Because losers confirm at **{los_conf_rate:.0f}%** (vs winners **{win_conf_rate:.0f}%**), the second-tranche
gate does {'' if result['useful_work'] else 'not '}separate the two populations — the deep tail losers
hold >60s and get the full blended position, so the tail is {'reduced' if result['cvar5_change_pp']<-1 else 'barely changed'}.
"""
    (SM / "staged_entry_summary.md").write_text(md, encoding="utf-8")
    print("  wrote staged_entry_summary.md")
    print(f"  staged {m_stag} | win_conf={win_conf_rate:.0f}% los_conf={los_conf_rate:.0f}%")


if __name__ == "__main__":
    main()
