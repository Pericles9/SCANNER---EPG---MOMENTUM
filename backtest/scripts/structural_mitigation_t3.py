#!/usr/bin/env python3
"""
Structural Mitigation — T3 Test A: MAE-gap disaster stop (reads T1 + T2 only).

Stop level = T2's 95th-pct winner MAE (single stated level, NOT swept). Recomputes each
trade's PnL from the stored MAE envelope: first new-low <= stop => exit there; else keep
baseline exit. Reports false-positive (winners cut) and true-positive (losers cut) counts.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

import tail_risk_lib as L

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
CHARTS = SM / "charts"


def apply_stop(tr, stop):
    """Return list of dicts with stopped pnl + cut flag for each trade."""
    out = []
    for r in tr:
        cut_pnl = None
        for t_rel, pnl, price in r["mae_envelope"]:
            if pnl <= stop:
                cut_pnl = pnl
                cut_t = t_rel
                break
        if cut_pnl is not None:
            out.append({**r, "stopped_pnl": float(cut_pnl), "was_cut": True, "cut_t": cut_t})
        else:
            out.append({**r, "stopped_pnl": r["baseline_pnl_pct"], "was_cut": False, "cut_t": None})
    return out


def metrics(pnls):
    p = np.array(pnls, dtype=float)
    return dict(n=len(p), pf=round(L.pf(p), 4), wr=round(L.wr(p), 2),
                mean=round(float(p.mean()), 3), cvar5=round(L.cvar5(p), 2))


def main():
    CHARTS.mkdir(parents=True, exist_ok=True)
    data = json.load(open(SM / "raw_trajectories.json"))
    tr = data["trades"]
    stop = json.load(open(SM / "t2_verdict.json"))["stop_level_pct"]
    ref = data["meta"]

    stopped = apply_stop(tr, stop)
    base_pnls = [r["baseline_pnl_pct"] for r in tr]
    stop_pnls = [r["stopped_pnl"] for r in stopped]

    cut = [r for r in stopped if r["was_cut"]]
    fp = [r for r in cut if r["is_winner"]]      # winners cut = false positives
    tp = [r for r in cut if not r["is_winner"]]  # losers cut = true positives
    made_worse = [r for r in cut if r["stopped_pnl"] < r["baseline_pnl_pct"] - 1e-6]
    made_better = [r for r in cut if r["stopped_pnl"] > r["baseline_pnl_pct"] + 1e-6]
    pnl_destroyed = round(sum(r["baseline_pnl_pct"] - r["stopped_pnl"] for r in made_worse), 1)
    shallow_losers_hurt = [r for r in tp if r["baseline_pnl_pct"] > stop]
    deep_losers_helped = [r for r in tp if r["baseline_pnl_pct"] < stop]

    m_base = metrics(base_pnls)
    m_stop = metrics(stop_pnls)
    tg300 = ref["tgate300_ref"]

    # Chart 1 — KDE baseline vs with-stop
    lo = min(min(base_pnls), min(stop_pnls)) - 3
    hi = max(max(base_pnls), max(stop_pnls)) + 3
    xs = np.linspace(lo, hi, 500)
    fig = go.Figure()
    for pnls, name, col in [(base_pnls, "Baseline (no stop)", "#78909C"),
                            (stop_pnls, f"With −{abs(stop)}% stop", "#EF5350")]:
        ys = L.kde_xy(pnls, xs)
        fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                      fill="tozeroy", fillcolor=L.hex_to_rgba(col, 0.08), name=name))
    for pnls, col, y in [(base_pnls, "#78909C", -0.002), (stop_pnls, "#EF5350", -0.005)]:
        fig.add_trace(go.Scatter(x=pnls, y=[y] * len(pnls), mode="markers",
                      marker=dict(color=col, size=6, symbol="line-ns-open"), showlegend=False))
    fig.add_vline(x=stop, line=dict(color="#FFEB3B", dash="dash"), annotation_text=f"stop={stop}%")
    fig.add_vline(x=0, line=dict(color="#888", dash="dot"))
    fig.update_layout(**L.base_layout(
        f"T3·1 · PnL% distribution: baseline vs −{abs(stop)}% disaster stop (ideal: only left tail moves)",
        "PnL%", "Density"))
    fig.write_html(str(CHARTS / "stop_pnl_kde.html"), include_plotlyjs="cdn")
    print("  wrote stop_pnl_kde.html")

    # Chart 2 — grouped bar PF/WR/mean/CVaR5, baseline vs stop vs T_gate=300
    _comparison_bar(
        "T3·2 · Baseline vs −{}% stop vs T_gate=300".format(abs(stop)),
        {"Baseline": m_base,
         f"−{abs(stop)}% stop": m_stop,
         "T_gate=300": dict(pf=tg300["pf"], wr=tg300["wr"], mean=tg300["mean_pnl"], cvar5=tg300["cvar5"])},
        CHARTS / "stop_comparison_bar.html")

    result = {
        "stop_level_pct": stop,
        "n_cut": len(cut),
        "false_positives_winners_cut": len(fp),
        "true_positives_losers_cut": len(tp),
        "fp_tickers": [f"{r['ticker']} {r['date']} ({r['baseline_pnl_pct']:+.1f}%→{r['stopped_pnl']:+.1f}%)" for r in fp],
        "baseline": m_base, "with_stop": m_stop, "tgate300": tg300,
        "cvar5_improvement_pp": round(m_stop["cvar5"] - m_base["cvar5"], 2),
        "wr_change_pp": round(m_stop["wr"] - m_base["wr"], 2),
        "mean_change": round(m_stop["mean"] - m_base["mean"], 3),
        "n_made_worse": len(made_worse), "n_made_better": len(made_better),
        "pnl_destroyed_pp": pnl_destroyed,
        "shallow_losers_hurt": len(shallow_losers_hurt),
        "deep_losers_helped": len(deep_losers_helped),
        "verdict": "NEGATIVE" if m_stop["pf"] < m_base["pf"] else "positive",
    }
    json.dump(result, open(SM / "t3_result.json", "w"), indent=2)

    m_base_loser_median = round(float(np.median([r["baseline_pnl_pct"] for r in tr if not r["is_winner"]])), 2)
    esc = "⚠ **ESCALATION FLAG**: false-positive count = {} (> 1)".format(len(fp)) if len(fp) > 1 else \
          "False-positive count = {} (within tolerance)".format(len(fp))
    md = f"""# T3 — Test A: MAE-gap disaster stop

Stop level = **{stop}%** (T2's 95th-pct winner MAE — single stated level, not swept).
Recomputed from the stored MAE envelope (T1). No parquet re-pull.

**Charts:** [stop_pnl_kde.html](charts/stop_pnl_kde.html) · [stop_comparison_bar.html](charts/stop_comparison_bar.html)

## T3a — Counts

- Trades cut by the stop: **{len(cut)}** of {len(tr)}
- **False positives (winners cut): {len(fp)}** — {result['fp_tickers']}
- True positives (losers cut): **{len(tp)}**

{esc} — {len(fp)} eventual winners round-trip through {stop}% before recovering; the MAE gap is
real but not perfectly clean (T2b already flagged this).

## Metrics

| config | n | PF | WR% | mean PnL% | CVaR5% |
|---|---|----|----|-----------|--------|
| Baseline (no stop) | {m_base['n']} | {m_base['pf']} | {m_base['wr']} | {m_base['mean']} | {m_base['cvar5']} |
| **−{abs(stop)}% stop** | {m_stop['n']} | **{m_stop['pf']}** | {m_stop['wr']} | {m_stop['mean']} | **{m_stop['cvar5']}** |
| T_gate=300 (ref) | 65 | {tg300['pf']} | {tg300['wr']} | {tg300['mean_pnl']} | {tg300['cvar5']} |

**CVaR5 {result['cvar5_improvement_pp']:+.2f}pp, WR {result['wr_change_pp']:+.2f}pp, mean {result['mean_change']:+.3f}pp vs baseline.**

## Verdict — NEGATIVE: the MAE gap is a mirage for a fixed stop

The stop **hurts** (PF {m_base['pf']}→{m_stop['pf']}, mean {m_base['mean']}→{m_stop['mean']}) and barely
touches the tail (CVaR5 only {result['cvar5_improvement_pp']:+.2f}pp). Of the {len(cut)} cut trades,
**{len(made_worse)} were made worse and only {len(made_better)} better** — the stop destroyed
**{pnl_destroyed}pp** of PnL through premature cuts. Root cause: **MAE is transient.** {len(shallow_losers_hurt)}
losers dipped through {stop}% intraday but *recovered* to a shallower final loss (median loser final
{m_base_loser_median}%); the stop converts those recoverable dips into locked-in ~{stop}% losses. It
genuinely rescues only **{len(deep_losers_helped)}** deep-tail trades — not enough to offset the
{len(shallow_losers_hurt)} shallow losers + {len(fp)} winners it clips. Winners rarely draw deep (T2),
but losers frequently draw deep *and bounce* — so "how deep price goes" does not separate the two at
exit. **Do not adopt a fixed MAE-gap stop.**
"""
    (SM / "stop_test_summary.md").write_text(md, encoding="utf-8")
    print("  wrote stop_test_summary.md")
    print(f"  cut={len(cut)} FP(winners)={len(fp)} TP(losers)={len(tp)}")
    print(f"  baseline {m_base} \n  with_stop {m_stop}")


def _comparison_bar(title, configs, path):
    metrics_order = ["pf", "wr", "mean", "cvar5"]
    labels = {"pf": "PF", "wr": "WR%", "mean": "mean PnL%", "cvar5": "CVaR5%"}
    colors = ["#78909C", "#EF5350", "#42A5F5", "#FFB74D", "#AB47BC"]
    fig = make_subplots(rows=1, cols=4, subplot_titles=[labels[m] for m in metrics_order])
    names = list(configs.keys())
    for ci, m in enumerate(metrics_order, start=1):
        for ni, name in enumerate(names):
            fig.add_trace(go.Bar(x=[name], y=[configs[name][m]], name=name,
                          marker_color=colors[ni % len(colors)], showlegend=(ci == 1),
                          text=[f"{configs[name][m]:.2f}"], textposition="outside"),
                          row=1, col=ci)
        if m == "pf":
            fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"), row=1, col=ci)
    fig.update_layout(template=L._DARK, width=1150, height=560,
                      title=dict(text=title, x=0.01, font=dict(size=14)),
                      legend=dict(orientation="h", y=1.08, font=dict(size=10)))
    fig.write_html(str(path), include_plotlyjs="cdn")
    print(f"  wrote {path.name}")


if __name__ == "__main__":
    main()
