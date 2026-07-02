#!/usr/bin/env python3
"""
Structural Mitigation — T5 Test C: spread-width entry guard (reads T1 only).

T5a diagnostic FIRST: entry bid-ask spread KDE, winners vs losers. Per the escalation
rule, if no real difference exists, skip T5b/T5c and report the null — do NOT force a
threshold. (Result: MWU p=0.74, effect 0.05 → null → T5b/c skipped.)
"""
from __future__ import annotations

import json
import numpy as np
import scipy.stats as sps
import plotly.graph_objects as go

import tail_risk_lib as L

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
CHARTS = SM / "charts"
W_COLOR, LZ_COLOR = "#66BB6A", "#EF5350"


def main():
    CHARTS.mkdir(parents=True, exist_ok=True)
    tr = json.load(open(SM / "raw_trajectories.json"))["trades"]
    W = np.array([r["entry_spread_pct"] for r in tr if r["is_winner"] and r["entry_spread_pct"] is not None])
    Lz = np.array([r["entry_spread_pct"] for r in tr if not r["is_winner"] and r["entry_spread_pct"] is not None])

    u, pval = sps.mannwhitneyu(W, Lz, alternative="two-sided")
    eff = abs(2 * u / (len(W) * len(Lz)) - 1)
    real_difference = bool(pval < 0.05 and eff > 0.2)

    # T5a chart — KDE overlay, x clipped to 0..p98 for readability, rug shows full range
    hi = float(np.percentile(np.concatenate([W, Lz]), 98)) + 0.5
    xs = np.linspace(0, hi, 400)
    fig = go.Figure()
    for data, name, col in [(W, f"Winners (n={len(W)})", W_COLOR),
                            (Lz, f"Losers (n={len(Lz)})", LZ_COLOR)]:
        ys = L.kde_xy(np.clip(data, 0, hi), xs)
        if ys is not None:
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                          fill="tozeroy", fillcolor=L.hex_to_rgba(col, 0.10),
                          name=f"{name}, med={np.median(data):.2f}%"))
        fig.add_trace(go.Scatter(x=np.clip(data, 0, hi), y=[-0.02] * len(data), mode="markers",
                      marker=dict(color=col, size=7, symbol="line-ns-open"), showlegend=False))
    fig.update_layout(**L.base_layout(
        f"T5a · Entry bid-ask spread%: winners vs losers — MWU p={pval:.2f}, effect={eff:.2f} (NULL)",
        "entry spread (% of mid)  [x clipped at p98; rug = all trades]", "Density"))
    fig.write_html(str(CHARTS / "spread_gap_kde.html"), include_plotlyjs="cdn")
    print("  wrote spread_gap_kde.html")

    result = dict(
        n_winners=len(W), n_losers=len(Lz),
        winner_spread_median=round(float(np.median(W)), 3),
        loser_spread_median=round(float(np.median(Lz)), 3),
        mwu_p=round(float(pval), 4), rank_biserial_effect=round(float(eff), 3),
        real_difference=real_difference,
        tail_trade_spreads=[dict(ticker=r["ticker"], spread=round(r["entry_spread_pct"], 3),
                                 pnl=round(r["baseline_pnl_pct"], 1))
                            for r in sorted(tr, key=lambda x: x["baseline_pnl_pct"])[:5]],
    )
    json.dump(result, open(SM / "t5_result.json", "w"), indent=2)

    md = f"""# T5 — Test C: Spread-width entry guard

**Chart:** [spread_gap_kde.html](charts/spread_gap_kde.html) (T5a diagnostic)

## T5a — Do winners and losers differ on entry spread? NO.

| group | n | median entry spread% |
|---|---|---|
| Winners | {len(W)} | {np.median(W):.3f}% |
| Losers | {len(Lz)} | {np.median(Lz):.3f}% |

Mann–Whitney **p = {pval:.3f}**, rank-biserial effect **{eff:.3f}** — no real difference. Winners'
median spread is actually marginally *wider* than losers'. The deepest tail losses had **tight**
spreads at entry (CNSP −27.4% @ 0.56%, BENF −12.5% @ 0.57%), so wide entry spread does not flag the
tail — if anything the worst trades looked liquid at entry.

## T5b / T5c — SKIPPED (escalation rule)

Per the phase escalation rule ("T5a finds no real spread difference → skip T5b/T5c, report null,
do not force a threshold"), no guard threshold is proposed and no filtered re-simulation is run.
**Null result.** A spread guard would exclude trades essentially at random with respect to outcome.
"""
    (SM / "spread_guard_summary.md").write_text(md, encoding="utf-8")
    print("  wrote spread_guard_summary.md")
    print(f"  T5a: MWU p={pval:.3f} eff={eff:.3f} -> real_difference={real_difference} (T5b/c skipped)")


if __name__ == "__main__":
    main()
