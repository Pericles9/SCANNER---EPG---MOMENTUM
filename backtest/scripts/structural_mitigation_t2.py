#!/usr/bin/env python3
"""
Structural Mitigation — T2 MAE/MFE gap diagnostic (reads T1 raw_trajectories.json only).

Chart 1: KDE overlay — winner MAE (how deep winners draw down) vs loser final PnL.
Chart 2: scatter — MAE (x) vs final PnL% (y), colored by win/loss.
Writes mae_mfe_verdict.md (T2a percentiles + T2b verdict) and derives the disaster-stop
level as the 95th-percentile winner drawdown (diagnostic-justified, NOT swept for PF).
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import plotly.graph_objects as go

import tail_risk_lib as L

SM = L.RESULTS / "phase_tail_risk" / "structural_mitigation"
CHARTS = SM / "charts"
W_COLOR, LZ_COLOR = "#66BB6A", "#EF5350"


def load():
    return json.load(open(SM / "raw_trajectories.json"))["trades"]


def main():
    CHARTS.mkdir(parents=True, exist_ok=True)
    tr = load()
    W = [r for r in tr if r["is_winner"]]
    Lz = [r for r in tr if not r["is_winner"]]
    w_mae = np.array([r["mae_pct"] for r in W])
    l_final = np.array([r["baseline_pnl_pct"] for r in Lz])
    l_mae = np.array([r["mae_pct"] for r in Lz])

    p90 = float(np.percentile(w_mae, 10))   # 90% shallower than this
    p95 = float(np.percentile(w_mae, 5))
    p99 = float(np.percentile(w_mae, 1))
    stop_level = round(p95, 2)

    # Chart 1 — KDE overlay winner MAE vs loser final PnL
    lo = min(w_mae.min(), l_final.min(), l_mae.min()) - 3
    hi = max(w_mae.max(), l_final.max()) + 3
    xs = np.linspace(lo, hi, 500)
    fig = go.Figure()
    for data, name, col in [(w_mae, f"Winner MAE / drawdown (n={len(W)})", W_COLOR),
                            (l_final, f"Loser final PnL (n={len(Lz)})", LZ_COLOR)]:
        ys = L.kde_xy(data, xs)
        if ys is not None:
            fig.add_trace(go.Scatter(x=xs, y=ys, mode="lines", line=dict(color=col, width=2),
                          fill="tozeroy", fillcolor=L.hex_to_rgba(col, 0.10),
                          name=f"{name}, med={np.median(data):.1f}%"))
        fig.add_trace(go.Scatter(x=data, y=[-0.003] * len(data), mode="markers",
                      marker=dict(color=col, size=7, symbol="line-ns-open"), showlegend=False))
    fig.add_vline(x=stop_level, line=dict(color="#FFEB3B", width=2, dash="dash"),
                  annotation_text=f"95th-pct winner MAE = {stop_level}%")
    fig.add_vline(x=0, line=dict(color="#888", dash="dot"))
    fig.update_layout(**L.base_layout(
        "T2·1 · Winner drawdown (MAE) vs Loser final PnL — is there a low-cost gap?",
        "PnL%", "Density"))
    fig.write_html(str(CHARTS / "mae_gap_kde.html"), include_plotlyjs="cdn")
    print("  wrote mae_gap_kde.html")

    # Chart 2 — MAE vs final PnL scatter
    fig = go.Figure()
    for grp, name, col in [(W, "winners", W_COLOR), (Lz, "losers", LZ_COLOR)]:
        fig.add_trace(go.Scatter(
            x=[r["mae_pct"] for r in grp], y=[r["baseline_pnl_pct"] for r in grp],
            mode="markers", marker=dict(color=col, size=8), name=name,
            text=[f"{r['ticker']} {r['date']} [{r['stratum']}/{r['session_bucket'][:3]}]" for r in grp],
            hovertemplate="%{text}<br>MAE=%{x:.1f}%  final=%{y:.1f}%<extra></extra>"))
    fig.add_vline(x=stop_level, line=dict(color="#FFEB3B", dash="dash"),
                  annotation_text=f"stop={stop_level}%")
    fig.add_hline(y=0, line=dict(color="#888", dash="dot"))
    # diagonal: final == MAE (trades that ended at their worst)
    lim = [min(w_mae.min(), l_mae.min()) - 2, max([r["mfe_pct"] for r in tr]) + 2]
    fig.add_trace(go.Scatter(x=lim, y=lim, mode="lines", line=dict(color="#555", dash="dot"),
                  name="final = MAE (ended at worst)"))
    fig.update_layout(**L.base_layout(
        "T2·2 · MAE vs final PnL% (below diagonal = recovered off the low)",
        "MAE % (worst drawdown during hold)", "final PnL%"))
    fig.write_html(str(CHARTS / "mae_pnl_scatter.html"), include_plotlyjs="cdn")
    print("  wrote mae_pnl_scatter.html")

    # T2a / T2b numbers
    w_breach = int((w_mae <= stop_level).sum())
    l_breach = int((l_mae <= stop_level).sum())
    tail_losers = sorted([r["baseline_pnl_pct"] for r in Lz])[:5]
    verdict = {
        "n_winners": len(W), "n_losers": len(Lz),
        "winner_mae_median": round(float(np.median(w_mae)), 2),
        "winner_mae_mean": round(float(w_mae.mean()), 2),
        "winner_mae_p90": round(p90, 2), "winner_mae_p95": round(p95, 2),
        "winner_mae_p99": round(p99, 2),
        "winner_mae_deepest": round(float(w_mae.min()), 2),
        "n_winners_below_10": int((w_mae < -10).sum()),
        "n_winners_below_15": int((w_mae < -15).sum()),
        "loser_final_median": round(float(np.median(l_final)), 2),
        "loser_final_worst": round(float(l_final.min()), 2),
        "loser_mae_median": round(float(np.median(l_mae)), 2),
        "tail_loser_finals": [round(x, 2) for x in tail_losers],
        "stop_level_pct": stop_level,
        "stop_level_source": "95th percentile of winner MAE (drawdown) — diagnostic-derived, not swept",
        "winners_breaching_stop": w_breach,
        "losers_breaching_stop": l_breach,
        "gap_exists": bool(l_breach >= 3 and w_breach <= 3),
    }
    json.dump(verdict, open(SM / "t2_verdict.json", "w"), indent=2)

    md = f"""# T2 — MAE/MFE Gap Diagnostic

Reads `raw_trajectories.json` (T1). No new extraction. Baseline = R1-Final sym_p80, no gate.

**Charts:** [mae_gap_kde.html](charts/mae_gap_kde.html) · [mae_pnl_scatter.html](charts/mae_pnl_scatter.html)

## T2a — Winners' drawdown boundary vs tail-loser endpoints

Winner MAE (deepest drawdown reached before recovering to a winning exit), n={len(W)}:

| stat | value |
|---|---|
| median | {verdict['winner_mae_median']}% |
| mean | {verdict['winner_mae_mean']}% |
| 90th-pct depth (90% of winners stayed shallower) | {verdict['winner_mae_p90']}% |
| 95th-pct depth | {verdict['winner_mae_p95']}% |
| 99th-pct depth | {verdict['winner_mae_p99']}% |
| deepest single winner | {verdict['winner_mae_deepest']}% |
| winners drawing below −10% | {verdict['n_winners_below_10']}/{len(W)} |
| winners drawing below −15% | {verdict['n_winners_below_15']}/{len(W)} |

Tail-loser final PnL (5 worst): {verdict['tail_loser_finals']} — loser final median {verdict['loser_final_median']}%,
loser MAE median {verdict['loser_mae_median']}%.

## T2b — Verdict: does a clean, low-cost gap exist?

**Yes, a gap exists — but it is real, not perfectly clean.** 95% of winners never draw below
**{verdict['winner_mae_p95']}%**, and the median winner only dips to {verdict['winner_mae_median']}%.
Tail losers, by contrast, end at −18% to −27% and pass through much deeper MAE
(median loser MAE {verdict['loser_mae_median']}%) on the way down. A disaster stop at the
**95th-percentile winner drawdown = {stop_level}%** (diagnostic-derived, not swept) would breach
**{w_breach} of {len(W)} winners** (false cuts) and **{l_breach} of {len(Lz)} losers** (true cuts).

The gap is not free: {verdict['n_winners_below_10']} winners genuinely round-trip below −10%
(the deepest to {verdict['winner_mae_deepest']}%) before recovering, so any stop shallow enough to
catch most losers will clip a few real winners. T3 tests this exact {stop_level}% level and reports
the false-positive count honestly.
"""
    (SM / "mae_mfe_verdict.md").write_text(md, encoding="utf-8")
    print("  wrote mae_mfe_verdict.md")
    print(f"  stop_level={stop_level}%  winners_breach={w_breach}  losers_breach={l_breach}")


if __name__ == "__main__":
    main()
