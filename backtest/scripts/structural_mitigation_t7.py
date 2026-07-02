#!/usr/bin/env python3
"""
Structural Mitigation — T7 consolidated comparison + phase summary.md.

Side-by-side: no-gate baseline, T_gate=300, T3 stop, T4 staged, T5 spread guard (null).
Reads the per-task result JSONs; no recomputation.
"""
from __future__ import annotations

import json
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import tail_risk_lib as L

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
CHARTS = SM / "charts"


def main():
    meta = json.load(open(SM / "raw_trajectories.json"))["meta"]
    t2 = json.load(open(SM / "t2_verdict.json"))
    t3 = json.load(open(SM / "t3_result.json"))
    t4 = json.load(open(SM / "t4_result.json"))
    t5 = json.load(open(SM / "t5_result.json"))
    base = meta["baseline_ref"]
    tg = meta["tgate300_ref"]

    configs = {
        "Baseline (no gate)": dict(pf=base["pf"], wr=base["wr"], mean=base["mean_pnl"], cvar5=base["cvar5"], n=65),
        "T_gate=300": dict(pf=tg["pf"], wr=tg["wr"], mean=tg["mean_pnl"], cvar5=tg["cvar5"], n=65),
        "T3 −9.59% stop": dict(pf=t3["with_stop"]["pf"], wr=t3["with_stop"]["wr"],
                               mean=t3["with_stop"]["mean"], cvar5=t3["with_stop"]["cvar5"], n=65),
        "T4 staged entry": dict(pf=t4["staged"]["pf"], wr=t4["staged"]["wr"],
                                mean=t4["staged"]["mean"], cvar5=t4["staged"]["cvar5"], n=65),
    }

    metrics_order = ["pf", "wr", "mean", "cvar5"]
    labels = {"pf": "PF", "wr": "WR%", "mean": "mean PnL%", "cvar5": "CVaR5%"}
    colors = {"Baseline (no gate)": "#78909C", "T_gate=300": "#26a69a",
              "T3 −9.59% stop": "#EF5350", "T4 staged entry": "#AB47BC"}
    fig = make_subplots(rows=1, cols=4, subplot_titles=[labels[m] for m in metrics_order])
    for ci, m in enumerate(metrics_order, start=1):
        for name, cfg in configs.items():
            fig.add_trace(go.Bar(x=[name], y=[cfg[m]], name=name, marker_color=colors[name],
                          showlegend=(ci == 1), text=[f"{cfg[m]:.2f}"], textposition="outside"),
                          row=1, col=ci)
        if m == "pf":
            fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), row=1, col=ci)
    fig.update_layout(template=L._DARK, width=1180, height=580,
                      title=dict(text="T7 · Consolidated: baseline vs T_gate=300 vs T3 stop vs T4 staged (T5=null)", x=0.01),
                      legend=dict(orientation="h", y=1.10, font=dict(size=10)))
    CHARTS.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(CHARTS / "final_comparison_bar.html"), include_plotlyjs="cdn")
    print("  wrote final_comparison_bar.html")

    md = f"""---
tags:
  - type/results
  - domain/backtest
  - project/hawkes-ofi-impact
  - status/complete
created: 2026-07-02
phase: Tail-Risk · Structural Mitigation
reference_config: "val_r4_stratified · p=0.80 · no-gate baseline"
---

# Structural Mitigation — Consolidated Verdict (T7)

Three prediction-free structural mitigations tested on the R1-Final (p=0.80, no-gate) baseline,
all recomputed from a single canonical dataset (`raw_trajectories.json`, T1; max reconciliation
error {meta['max_recon_err_pct']}%). Anti-overfitting rule honoured: every threshold is
diagnostic-derived or an explicit convention — none swept for PF/CVaR5.

**Chart:** [final_comparison_bar.html](charts/final_comparison_bar.html)

| config | n | PF | WR% | mean PnL% | CVaR5% | verdict |
|---|---|----|----|-----------|--------|---------|
| Baseline (no gate) | 65 | {base['pf']} | {base['wr']} | {base['mean_pnl']} | {base['cvar5']} | reference |
| **T_gate=300** (R1.5-Final) | 65 | **{tg['pf']}** | {tg['wr']} | {tg['mean_pnl']} | **{tg['cvar5']}** | only candidate cutting the tail at acceptable cost |
| T3 — −9.59% MAE stop | 65 | {t3['with_stop']['pf']} | {t3['with_stop']['wr']} | {t3['with_stop']['mean']} | {t3['with_stop']['cvar5']} | **NEGATIVE** |
| T4 — staged entry | 65 | {t4['staged']['pf']} | {t4['staged']['wr']} | {t4['staged']['mean']} | {t4['staged']['cvar5']} | **NEGATIVE** |
| T5 — spread guard | — | — | — | — | — | **NULL** (T5a no signal, not tested) |

## Which candidate cut tail risk with minimal cost?

- **T3 disaster stop — abandon.** PF {base['pf']}→{t3['with_stop']['pf']}, mean {base['mean_pnl']}→{t3['with_stop']['mean']},
  CVaR5 only {t3['cvar5_improvement_pp']:+.2f}pp. The MAE gap (T2) is real but a *mirage for a fixed stop*:
  MAE is transient — {t3['shallow_losers_hurt']} losers dip through −9.59% and recover to shallower final
  losses, so the stop locks in ~−10% on recoverable trades and clips {t3['false_positives_winners_cut']}
  winners, destroying {t3['pnl_destroyed_pp']}pp to rescue only {t3['deep_losers_helped']} deep-tail trades.
- **T4 staged entry — abandon.** PF {base['pf']}→{t4['staged']['pf']}, and CVaR5 gets **worse**
  ({t4['cvar5_change_pp']:+.2f}pp). Confirmation is non-selective: winners confirm at {t4['win_confirm_rate']}%,
  losers at {t4['los_confirm_rate']}% (losers *more*). Deep losers bleed slowly (>60s), pass the checkpoint,
  and receive the second tranche — the mechanism adds to the tail while giving up EV on early winners.
- **T5 spread guard — abandon (null).** Entry spread does not separate winners from losers
  (MWU p={t5['mwu_p']}, effect {t5['rank_biserial_effect']}); the deepest losses had the *tightest* spreads.

## Takeaway

All three prediction-free structural mitigations fail. The common thread across this project's
now-four failed tail attempts (SF-entry, ROC gate, volume-at-entry, and this trio) is that
**losers on these names are not distinguishable from winners at or shortly after entry — by price
level (MAE), by momentum confirmation (60s hold), or by liquidity (spread).** The only mechanism
that reduces CVaR5 without wrecking PF remains the **time-gate exit at 300s** (R1.5-Final), which
cuts on realized adverse P&L after a fixed clock rather than trying to predict. [[project-phase-state]]

No design decision made here. Cooper decides whether to adopt T_gate=300 and formally shelve the
three structural candidates.

## Escalation Check

| Condition | Result |
|---|---|
| Required field missing | CLEARED — T1 reconciles to baseline at {meta['max_recon_err_pct']}% error |
| Threshold selected by sweeping for best PF/CVaR5 | CLEARED — stop=95th-pct winner MAE (diagnostic); staged checkpoint=60s (round number); spread guard not proposed (null) |
| T3 false-positive count > 1 | **FLAGGED** — FP={t3['false_positives_winners_cut']} winners cut; reported openly, not hidden |
| T5a finds no spread difference | Triggered → T5b/c skipped, null reported |
"""
    (SM / "summary.md").write_text(md, encoding="utf-8")
    print("  wrote summary.md")


if __name__ == "__main__":
    main()
