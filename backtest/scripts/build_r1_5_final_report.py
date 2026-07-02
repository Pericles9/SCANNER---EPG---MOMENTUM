#!/usr/bin/env python3
"""
Phase R1.5-Final — build sweep table + 3 charts + summary from tg{N} runs.

Reads results/phase_r1_5_final/tg{300,400,500,600,750}/run_summary.json.
Writes sweep_table.md and charts/{pf_cvar_pnl,session_pf,exit_reason}_vs_tgate.html.
"""
from __future__ import annotations

import json
from pathlib import Path

import plotly.graph_objects as go
from plotly.subplots import make_subplots

BASE = Path(__file__).resolve().parent.parent.parent
RESULTS = BASE / "backtest" / "results" / "phase_r1_5_final"
CHARTS = RESULTS / "charts"
T_GATES = [300, 400, 500, 600, 750]
_DARK = "plotly_dark"
EXIT_ORDER = ["time_gate", "epg_window_close", "session_end"]
EXIT_COLORS = {"time_gate": "#EF5350", "epg_window_close": "#42A5F5", "session_end": "#FFB74D"}


def load_rows():
    rows = []
    for tg in T_GATES:
        p = RESULTS / f"tg{tg}" / "run_summary.json"
        if not p.exists():
            print(f"  ! missing {p}")
            continue
        d = json.load(open(p))
        sb = d.get("session_breakdown", {})
        rth = sb.get("regular_hours", {})
        pre = sb.get("pre_market", {})
        eb = d.get("exit_reason_breakdown", {})
        exit_pct = {r: round(eb.get(r, {}).get("pct_of_trades", 0.0), 1) for r in EXIT_ORDER}
        rows.append(dict(
            tg=tg, n=d["n_trades"], pf=d["profit_factor"], wr=d["win_rate"],
            mean=d["mean_pnl_pct"], cvar5=d["cvar5_pct"],
            rth_n=rth.get("n_trades", 0), rth_pf=rth.get("profit_factor", 0.0),
            pre_n=pre.get("n_trades", 0), pre_pf=pre.get("profit_factor", 0.0),
            p90_lag=d.get("p90_entry_lag_from_scanner_sec"),
            exit_pct=exit_pct,
            tg_count=eb.get("time_gate", {}).get("count", 0),
            tg_pf=eb.get("time_gate", {}).get("profit_factor"),
            tg_mean=eb.get("time_gate", {}).get("mean_pnl_pct"),
        ))
    return rows


def write_table(rows):
    lines = [
        "# Phase R1.5-Final — T_gate (time-gate exit) sweep table",
        "",
        "Sample: `val_r4_stratified.json` (n=100, mom_pct tercile strata, 30/40/30, seed=42)  ",
        "Fixed: p_open=p_close=0.80, max_entry_lag_sec=500, entry_mode=first_pass  ",
        "Swept: `--t-gate-sec` (single-shot: at first tick ≥ T_gate since entry, if open P&L<0 → exit)",
        "",
        "| T_gate | n | PF | WR% | mean PnL% | CVaR5% | RTH n/PF | PRE n/PF | p90 lag(s) | window_close% | time_gate% | session_end% |",
        "|--------|---|----|----|-----------|--------|----------|----------|------------|---------------|------------|--------------|",
    ]
    for r in rows:
        ep = r["exit_pct"]
        lines.append(
            f"| {r['tg']} | {r['n']} | {r['pf']:.4f} | {r['wr']:.2f} | {r['mean']:.3f} | "
            f"{r['cvar5']:.2f} | {r['rth_n']}/{r['rth_pf']:.3f} | {r['pre_n']}/{r['pre_pf']:.3f} | "
            f"{r['p90_lag']:.0f} | {ep['epg_window_close']:.1f} | {ep['time_gate']:.1f} | {ep['session_end']:.1f} |"
        )
    lines += ["", "### Time-gate exits (when they fire)",
              "", "| T_gate | time_gate count | time_gate PF | time_gate mean PnL% |",
              "|--------|-----------------|--------------|---------------------|"]
    for r in rows:
        pf = f"{r['tg_pf']:.4f}" if r["tg_pf"] is not None else "—"
        mn = f"{r['tg_mean']:.3f}" if r["tg_mean"] is not None else "—"
        lines.append(f"| {r['tg']} | {r['tg_count']} | {pf} | {mn} |")
    (RESULTS / "sweep_table.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"  wrote sweep_table.md")


def chart1(rows):
    tg = [r["tg"] for r in rows]
    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(go.Scatter(x=tg, y=[r["pf"] for r in rows], name="PF",
                  mode="lines+markers", line=dict(color="#42A5F5", width=2.5)), secondary_y=False)
    fig.add_trace(go.Scatter(x=tg, y=[r["mean"] for r in rows], name="mean PnL%",
                  mode="lines+markers", line=dict(color="#66BB6A", width=2)), secondary_y=False)
    fig.add_trace(go.Scatter(x=tg, y=[r["cvar5"] for r in rows], name="CVaR5%",
                  mode="lines+markers", line=dict(color="#EF5350", width=2, dash="dot")), secondary_y=True)
    fig.add_vline(x=500, line=dict(color="#888", dash="dash"),
                  annotation_text="R1.5 incumbent (500)")
    fig.update_layout(template=_DARK, width=1050, height=600,
                      title=dict(text="R1.5-Final · PF / mean PnL / CVaR5 vs T_gate (p=0.80)", x=0.01),
                      xaxis_title="T_gate (s)", legend=dict(x=0.01, y=0.99, bgcolor="rgba(0,0,0,0.3)"))
    fig.update_yaxes(title_text="PF / mean PnL%", secondary_y=False)
    fig.update_yaxes(title_text="CVaR5%", secondary_y=True)
    CHARTS.mkdir(parents=True, exist_ok=True)
    fig.write_html(str(CHARTS / "pf_cvar_pnl_vs_tgate.html"), include_plotlyjs="cdn")
    print("  wrote pf_cvar_pnl_vs_tgate.html")


def chart2(rows):
    tg = [r["tg"] for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=tg, y=[r["rth_pf"] for r in rows], name="RTH PF",
                  mode="lines+markers", line=dict(color="#26a69a", width=2.5),
                  text=[f"n={r['rth_n']}" for r in rows]))
    fig.add_trace(go.Scatter(x=tg, y=[r["pre_pf"] for r in rows], name="Pre-market PF",
                  mode="lines+markers", line=dict(color="#FFB74D", width=2.5),
                  text=[f"n={r['pre_n']}" for r in rows]))
    fig.add_hline(y=1.0, line=dict(color="#888", dash="dash"))
    fig.add_vline(x=500, line=dict(color="#888", dash="dash"),
                  annotation_text="incumbent (500)")
    fig.update_layout(template=_DARK, width=1050, height=600,
                      title=dict(text="R1.5-Final · PF by session vs T_gate (p=0.80)", x=0.01),
                      xaxis_title="T_gate (s)", yaxis_title="Profit Factor",
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(0,0,0,0.3)"))
    fig.write_html(str(CHARTS / "session_pf_vs_tgate.html"), include_plotlyjs="cdn")
    print("  wrote session_pf_vs_tgate.html")


def chart3(rows):
    tg = [str(r["tg"]) for r in rows]
    fig = go.Figure()
    for reason in EXIT_ORDER:
        fig.add_trace(go.Bar(x=tg, y=[r["exit_pct"][reason] for r in rows],
                      name=reason, marker_color=EXIT_COLORS[reason],
                      text=[f"{r['exit_pct'][reason]:.0f}%" for r in rows], textposition="inside"))
    fig.update_layout(template=_DARK, width=1050, height=600, barmode="stack",
                      title=dict(text="R1.5-Final · Exit-reason composition vs T_gate (p=0.80)", x=0.01),
                      xaxis_title="T_gate (s)", yaxis_title="% of trades",
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(0,0,0,0.3)"))
    fig.write_html(str(CHARTS / "exit_reason_vs_tgate.html"), include_plotlyjs="cdn")
    print("  wrote exit_reason_vs_tgate.html")


def main():
    rows = load_rows()
    if not rows:
        print("No runs found."); return
    write_table(rows)
    chart1(rows); chart2(rows); chart3(rows)
    json.dump(rows, open(RESULTS / "sweep_rows.json", "w"), indent=2, default=str)
    print("Done.")
    # quick console view
    for r in rows:
        print(f"  T{r['tg']}: n={r['n']} PF={r['pf']:.3f} CVaR5={r['cvar5']:.2f} "
              f"PRE_PF={r['pre_pf']:.3f} time_gate%={r['exit_pct']['time_gate']} (n={r['tg_count']})")


if __name__ == "__main__":
    main()
