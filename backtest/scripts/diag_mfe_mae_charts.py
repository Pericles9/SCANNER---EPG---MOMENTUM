#!/usr/bin/env python3
"""Chart + summary builder for Phase DIAG-MFE-MAE. Imported by diag_mfe_mae.py."""
from __future__ import annotations

import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots

GREEN = "#2ca02c"
RED = "#d62728"
GREEN_F = "rgba(44,160,44,0.5)"
RED_F = "rgba(214,39,40,0.5)"

_LAYOUT = dict(width=1200, paper_bgcolor="white", plot_bgcolor="#fafafa",
               xaxis_rangeslider_visible=False,
               legend=dict(bgcolor="rgba(255,255,255,0.6)", bordercolor="#ccc",
                           borderwidth=1, x=0.99, y=0.99, xanchor="right", yanchor="top"))


def _sizes(holds, lo=6, hi=20):
    h = np.asarray(holds, dtype=float)
    if h.max() == h.min():
        return np.full(len(h), (lo + hi) / 2)
    return lo + (h - h.min()) / (h.max() - h.min()) * (hi - lo)


def _scatter(metrics, xk, yk, xlabel, ylabel, title, path):
    holds = [m["hold_sec"] for m in metrics]
    sz = _sizes(holds)
    fig = go.Figure()
    for win, color, name in [(True, GREEN, "Wins"), (False, RED, "Losses")]:
        idx = [i for i, m in enumerate(metrics)
               if m["win"] == win and m[xk] is not None and m[yk] is not None]
        if not idx:
            continue
        fig.add_trace(go.Scatter(
            x=[metrics[i][xk] for i in idx], y=[metrics[i][yk] for i in idx],
            mode="markers",
            marker=dict(color=color, size=[sz[i] for i in idx],
                        line=dict(width=0.5, color="#333"), opacity=0.8),
            name=f"{name} (N={len(idx)})",
            customdata=[[metrics[i]["ticker"], metrics[i]["date"],
                         metrics[i]["pnl_pct"], metrics[i]["hold_sec"],
                         metrics[i][xk], metrics[i][yk]] for i in idx],
            hovertemplate=("%{customdata[0]} %{customdata[1]}<br>"
                           "pnl=%{customdata[2]:.2f}%  hold=%{customdata[3]:.0f}s<br>"
                           f"{xlabel}=%{{customdata[4]:.3f}}  {ylabel}=%{{customdata[5]:.3f}}"
                           "<extra></extra>")))
    allx = [m[xk] for m in metrics if m[xk] is not None]
    ally = [m[yk] for m in metrics if m[yk] is not None]
    lo = min(min(allx), min(ally)); hi = max(max(allx), max(ally))
    fig.add_trace(go.Scatter(x=[lo, hi], y=[lo, hi], mode="lines",
                  line=dict(color="gray", dash="dash", width=1),
                  name="MFE = MAE", hoverinfo="skip"))
    fig.update_layout(title=title, height=700, **_LAYOUT)
    fig.update_xaxes(title_text=xlabel, gridcolor="#e5e5e5", zeroline=True,
                     zerolinecolor="#bbb")
    fig.update_yaxes(title_text=ylabel, gridcolor="#e5e5e5", zeroline=True,
                     zerolinecolor="#bbb")
    fig.write_html(str(path), include_plotlyjs="cdn")


def _hist_mae(metrics, stops, opt2_x, path):
    win_v = [m["mae_gap_fraction"] for m in metrics if m["win"] and m["mae_gap_fraction"] is not None]
    los_v = [m["mae_gap_fraction"] for m in metrics if not m["win"] and m["mae_gap_fraction"] is not None]
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=win_v, name=f"Wins (N={len(win_v)})",
                  marker_color=GREEN_F, xbins=dict(size=0.05)))
    fig.add_trace(go.Histogram(x=los_v, name=f"Losses (N={len(los_v)})",
                  marker_color=RED_F, xbins=dict(size=0.05)))
    fsr = {round(s["value"], 2): s["false_stop_rate"] for s in stops if s["type"] == "Option 2"}
    for x in opt2_x:
        rate = fsr.get(round(x, 2))
        rate_s = f"{rate*100:.0f}%" if rate is not None else "n/a"
        fig.add_vline(x=x, line=dict(color="#444", dash="dash", width=1),
                      annotation_text=f"X={x:.2f} | false {rate_s}",
                      annotation_position="top", annotation_textangle=-90,
                      annotation_font_size=10)
    fig.update_layout(title="MAE Distribution — Gap Fraction | Option 2 candidate stop lines",
                      barmode="overlay", height=700, **_LAYOUT)
    fig.update_xaxes(title_text="MAE (gap fraction)", gridcolor="#e5e5e5")
    fig.update_yaxes(title_text="Count", gridcolor="#e5e5e5")
    fig.write_html(str(path), include_plotlyjs="cdn")


def _hist_mfe(metrics, path):
    win_v = [m["mfe_gap_fraction"] for m in metrics if m["win"] and m["mfe_gap_fraction"] is not None]
    los_v = [m["mfe_gap_fraction"] for m in metrics if not m["win"] and m["mfe_gap_fraction"] is not None]
    fig = go.Figure()
    fig.add_trace(go.Histogram(x=win_v, name=f"Wins (N={len(win_v)})",
                  marker_color=GREEN_F, xbins=dict(size=0.05)))
    fig.add_trace(go.Histogram(x=los_v, name=f"Losses (N={len(los_v)})",
                  marker_color=RED_F, xbins=dict(size=0.05)))
    if win_v:
        mw = float(np.mean(win_v))
        fig.add_vline(x=mw, line=dict(color=GREEN, width=2),
                      annotation_text=f"mean win MFE={mw:.2f}",
                      annotation_position="top", annotation_font_color=GREEN)
    if los_v:
        ml = float(np.mean(los_v))
        fig.add_vline(x=ml, line=dict(color=RED, width=2),
                      annotation_text=f"mean loss MFE={ml:.2f}",
                      annotation_position="bottom", annotation_font_color=RED)
    fig.update_layout(title="MFE Distribution — Gap Fraction | p=0.65 arm",
                      barmode="overlay", height=700, **_LAYOUT)
    fig.update_xaxes(title_text="MFE (gap fraction)", gridcolor="#e5e5e5")
    fig.update_yaxes(title_text="Count", gridcolor="#e5e5e5")
    fig.write_html(str(path), include_plotlyjs="cdn")


def _table(stops, path, actual_pf, actual_cvar5):
    def fcol(v):
        if v is None:
            return "#fff"
        if v < 0.20:
            return "#c8e6c9"
        if v <= 0.40:
            return "#fff9c4"
        return "#ffcdd2"

    def dcol(v):
        if v is None:
            return "#fff"
        return "#c8e6c9" if v > 0 else "#ffcdd2"

    rows = []
    # No-stop row
    rows.append(f"""<tr style="font-weight:bold;background:#eee">
<td>No stop</td><td>—</td><td>0.0%</td><td>0.0%</td><td>0.0%</td>
<td>{actual_pf:.4f}</td><td>0.0000</td><td>{actual_cvar5:.2f}%</td></tr>""")
    for s in stops:
        fr = s["false_stop_rate"]; tr = s["true_stop_rate"]
        pfd = s["pf_delta"]; simpf = s["simulated_pf"]
        lvl = (f"buffer={s['value']:.2f}" if s["type"] == "Option 1"
               else f"X={s['value']:.2f}")
        simpf_s = f"{simpf:.4f}" if simpf is not None else "null (no losses)"
        pfd_s = f"{pfd:+.4f}" if pfd is not None else "—"
        rows.append(f"""<tr>
<td>{s['type']}</td><td>{lvl}</td>
<td>{s['stop_hit_rate']*100:.1f}%</td>
<td style="background:{fcol(fr)}">{fr*100:.1f}%</td>
<td>{tr*100:.1f}%</td>
<td>{simpf_s}</td>
<td style="background:{dcol(pfd)}">{pfd_s}</td>
<td>{s['simulated_cvar5_pct']:.2f}%</td></tr>""")
    body = "\n".join(rows)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Stop Level Simulation — p=0.65 arm</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;padding:24px;background:#fff}}
h2{{color:#222}}
table{{border-collapse:collapse;width:100%;max-width:1100px;font-size:14px}}
th,td{{border:1px solid #ccc;padding:7px 12px;text-align:right}}
th{{background:#37474f;color:#fff;text-align:center}}
td:first-child,td:nth-child(2){{text-align:left}}
caption{{caption-side:top;font-size:12px;color:#666;padding-bottom:8px}}
</style></head><body>
<h2>Stop Level Simulation — p=0.65 arm, 46 trades</h2>
<table>
<caption>False Stop% shaded: green &lt;20%, yellow 20–40%, red &gt;40%. PF Δ shaded green if positive.
CVaR5 = mean of most-negative 5% (same method as runner). Win/loss = actual final PnL.</caption>
<thead><tr>
<th>Stop Type</th><th>Level</th><th>Stop Hit%</th><th>False Stop% (of wins)</th>
<th>True Stop% (of losses)</th><th>Sim PF</th><th>PF Δ</th><th>Sim CVaR5</th>
</tr></thead><tbody>
{body}
</tbody></table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _spaghetti(series, path):
    max_h = max(len(s["open_pnl"]) for s in series)
    grid = np.arange(max_h)
    fig = go.Figure()
    for s in series:
        op = s["open_pnl"]
        color = GREEN if s["win"] else RED
        fig.add_trace(go.Scatter(
            x=np.arange(len(op)), y=op, mode="lines",
            line=dict(color=color, width=0.8), opacity=0.35,
            name=f"{s['ticker']} {s['date']}", showlegend=False,
            hovertemplate=(f"{s['ticker']} {s['date']}<br>"
                           f"final pnl={s['pnl_pct']:.2f}%<br>"
                           "t=%{x}s  open=%{y:.2f}%<extra></extra>")))
    mean_line = []
    for t in grid:
        vals = [s["open_pnl"][t] for s in series if t < len(s["open_pnl"])]
        mean_line.append(np.mean(vals) if vals else np.nan)
    fig.add_trace(go.Scatter(x=grid, y=mean_line, mode="lines",
                  line=dict(color="black", width=2.5), name="Mean (open trades)"))
    fig.add_hline(y=0.0, line=dict(color="gray", dash="dash", width=1))
    fig.add_vline(x=300, line=dict(color="orange", dash="dash", width=1.5),
                  annotation_text="max_lag=300s", annotation_position="top")
    fig.update_layout(title="Open P&L Spaghetti — All 46 Trades | p=0.65 arm",
                      height=700, showlegend=True, **_LAYOUT)
    fig.update_xaxes(title_text="Seconds since entry", gridcolor="#e5e5e5")
    fig.update_yaxes(title_text="Open P&L (%)", gridcolor="#e5e5e5")
    fig.write_html(str(path), include_plotlyjs="cdn")


def _bands(series, path):
    max_h = max(len(s["open_pnl"]) for s in series)
    fig = go.Figure()
    last_n = {"Winners": 0, "Losers": 0}
    for win, color, fillc, name in [
            (True, GREEN, "rgba(44,160,44,0.18)", "Winners"),
            (False, RED, "rgba(214,39,40,0.18)", "Losers")]:
        sub = [s for s in series if s["win"] == win]
        ts, means, p25, p75 = [], [], [], []
        for t in range(max_h):
            vals = [s["open_pnl"][t] for s in sub if t < len(s["open_pnl"])]
            if len(vals) >= 3:
                ts.append(t); means.append(np.mean(vals))
                p25.append(np.percentile(vals, 25)); p75.append(np.percentile(vals, 75))
        if not ts:
            continue
        last_n[name] = sum(1 for s in sub if (ts[-1]) < len(s["open_pnl"]))
        fig.add_trace(go.Scatter(x=ts + ts[::-1], y=p75 + p25[::-1], fill="toself",
                      fillcolor=fillc, line=dict(width=0), hoverinfo="skip",
                      name=f"{name} IQR", showlegend=True))
        fig.add_trace(go.Scatter(x=ts, y=means, mode="lines",
                      line=dict(color=color, width=2.5), name=f"{name} mean"))
    fig.add_hline(y=0.0, line=dict(color="gray", dash="dash", width=1))
    fig.add_vline(x=300, line=dict(color="orange", dash="dash", width=1.5),
                  annotation_text="max_lag=300s", annotation_position="top")
    fig.add_annotation(xref="paper", yref="paper", x=0.99, y=0.02,
                       text=f"N open at series end — Winners:{last_n['Winners']}  Losers:{last_n['Losers']}",
                       showarrow=False, font=dict(size=11, color="#444"),
                       bgcolor="rgba(255,255,255,0.7)")
    fig.update_layout(title="Mean P&L ± IQR Band — Winners vs Losers | p=0.65 arm",
                      height=700, **_LAYOUT)
    fig.update_xaxes(title_text="Seconds since entry", gridcolor="#e5e5e5")
    fig.update_yaxes(title_text="Open P&L (%)", gridcolor="#e5e5e5")
    fig.write_html(str(path), include_plotlyjs="cdn")


def _timing(metrics, path):
    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, vertical_spacing=0.08,
                        subplot_titles=["t_MAE relative position in hold",
                                        "t_MFE relative position in hold"])
    for r, key in [(1, "t_mae_relative"), (2, "t_mfe_relative")]:
        wv = [m[key] for m in metrics if m["win"]]
        lv = [m[key] for m in metrics if not m["win"]]
        fig.add_trace(go.Histogram(x=wv, name="Wins", marker_color=GREEN_F,
                      xbins=dict(size=0.10, start=0, end=1.0001), showlegend=(r == 1),
                      legendgroup="w"), row=r, col=1)
        fig.add_trace(go.Histogram(x=lv, name="Losses", marker_color=RED_F,
                      xbins=dict(size=0.10, start=0, end=1.0001), showlegend=(r == 1),
                      legendgroup="l"), row=r, col=1)
        mw = float(np.mean(wv)) if wv else 0.0
        ml = float(np.mean(lv)) if lv else 0.0
        fig.add_annotation(x=0.02, y=0.99, xref=f"x{r if r>1 else ''} domain",
                           yref=f"y{r if r>1 else ''} domain",
                           text=f"mean wins={mw:.2f}", showarrow=False,
                           font=dict(color=GREEN, size=12), xanchor="left")
        fig.add_annotation(x=0.02, y=0.85, xref=f"x{r if r>1 else ''} domain",
                           yref=f"y{r if r>1 else ''} domain",
                           text=f"mean losses={ml:.2f}", showarrow=False,
                           font=dict(color=RED, size=12), xanchor="left")
    fig.update_layout(title="MAE / MFE Timing | When do adverse and favorable extremes occur?",
                      barmode="overlay", height=900, **_LAYOUT)
    fig.update_xaxes(title_text="relative position (0=entry, 1=exit)", row=2, col=1)
    fig.update_yaxes(title_text="Count", row=1, col=1)
    fig.update_yaxes(title_text="Count", row=2, col=1)
    fig.write_html(str(path), include_plotlyjs="cdn")


CHART_DEFS = [
    ("chart_01_mfe_mae_scatter_gap.html", "MFE vs MAE scatter in gap-fraction space; size=hold, diagonal MFE=MAE"),
    ("chart_02_mfe_mae_scatter_pct.html", "MFE vs MAE scatter in raw % space"),
    ("chart_03_mae_distribution_gap.html", "MAE histogram (wins/losses) with Option 2 candidate stop lines + false-stop rates"),
    ("chart_04_mfe_distribution_gap.html", "MFE histogram (wins/losses) with mean-MFE lines"),
    ("chart_05_stop_simulation_table.html", "Stop simulation table — all 9 levels + no-stop baseline"),
    ("chart_06_pnl_spaghetti.html", "Open P&L per trade vs seconds-since-entry, mean over open trades"),
    ("chart_07_pnl_bands.html", "Mean P&L ± IQR band, winners vs losers"),
    ("chart_08_timing_distributions.html", "t_MAE / t_MFE relative-timing histograms (2-panel)"),
]


def _index(path):
    rows = "\n".join(
        f'<tr><td>Chart {i+1}</td><td>{desc}</td>'
        f'<td><a href="{fn}" target="_blank">{fn}</a></td></tr>'
        for i, (fn, desc) in enumerate(CHART_DEFS))
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>DIAG-MFE-MAE — chart index</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;padding:24px}}
table{{border-collapse:collapse;width:100%;max-width:1000px}}
th,td{{border:1px solid #ccc;padding:8px 12px;text-align:left}}
th{{background:#37474f;color:#fff}} a{{color:#1565c0;text-decoration:none}}
tr:hover{{background:#f5f5f5}}
</style></head><body>
<h2>Phase DIAG-MFE-MAE — Trade Geometry &amp; Stop Diagnostic (p=0.65 arm, 46 trades)</h2>
<table><thead><tr><th>Chart</th><th>Description</th><th>File</th></tr></thead>
<tbody>{rows}</tbody></table></body></html>"""
    with open(path, "w", encoding="utf-8") as f:
        f.write(html)


def _summary(metrics, stops, n_wins, n_losses, t1c, out, actual_pf, actual_cvar5):
    def agg(key, sub):
        vals = [m[key] for m in sub if m[key] is not None]
        return (float(np.mean(vals)) if vals else None,
                float(np.median(vals)) if vals else None)
    wins = [m for m in metrics if m["win"]]
    los = [m for m in metrics if not m["win"]]
    mfe_b4_mae_all = sum(1 for m in metrics if m["mfe_before_mae"]) / len(metrics)
    mfe_b4_mae_los = (sum(1 for m in los if m["mfe_before_mae"]) / len(los)) if los else 0.0

    def first_below(opt_type, thresh=0.25):
        cand = [s for s in stops if s["type"] == opt_type]
        cand.sort(key=lambda s: s["value"])
        for s in cand:
            if s["false_stop_rate"] is not None and s["false_stop_rate"] < thresh:
                return s
        return None

    o1 = first_below("Option 1")
    o2 = first_below("Option 2")

    def lvl_s(s):
        if s is None:
            return "none below 25%"
        return (f"buffer={s['value']:.2f} (false {s['false_stop_rate']*100:.0f}%, "
                f"true {s['true_stop_rate']*100:.0f}%, PFΔ {s['pf_delta']:+.3f})"
                if s["type"] == "Option 1"
                else f"X={s['value']:.2f} (false {s['false_stop_rate']*100:.0f}%, "
                f"true {s['true_stop_rate']*100:.0f}%, PFΔ {s['pf_delta']:+.3f})")

    mae_pct_all = agg("mae_pct", metrics); mfe_pct_all = agg("mfe_pct", metrics)
    mae_gf_all = agg("mae_gap_fraction", metrics); mfe_gf_all = agg("mfe_gap_fraction", metrics)

    lines = []
    lines.append("# Phase DIAG-MFE-MAE — Summary\n")
    lines.append("**Date:** 2026-06-24")
    lines.append("**Source:** R1-Fixed p=0.65 arm — 46 trades, all `epg_window_close` exits, "
                 f"actual PF={actual_pf}, actual CVaR5={actual_cvar5}%.")
    lines.append("**Analysis-only.** No backtest run, no parameter/code change. "
                 "Per-event chart requirement waived (Agent_Prompt_Standard.md §7).\n")
    lines.append("## Key stats\n")
    lines.append(f"- N trades = {len(metrics)}; wins = {n_wins}; losses = {n_losses}.")
    lines.append(f"- MAE: mean {mae_pct_all[0]*100:.2f}% / median {mae_pct_all[1]*100:.2f}% (raw); "
                 f"mean {mae_gf_all[0]:.3f} / median {mae_gf_all[1]:.3f} (gap fraction).")
    lines.append(f"- MFE: mean {mfe_pct_all[0]*100:.2f}% / median {mfe_pct_all[1]*100:.2f}% (raw); "
                 f"mean {mfe_gf_all[0]:.3f} / median {mfe_gf_all[1]:.3f} (gap fraction).")
    lines.append(f"- Mean t_MAE_relative = {agg('t_mae_relative', metrics)[0]:.3f}; "
                 f"mean t_MFE_relative = {agg('t_mfe_relative', metrics)[0]:.3f} "
                 "(0=entry, 1=exit).\n")
    lines.append("## MFE-before-MAE rate\n")
    lines.append(f"- All trades: {mfe_b4_mae_all*100:.1f}% saw their MFE before their MAE.")
    lines.append(f"- Losses only: {mfe_b4_mae_los*100:.1f}% of losing trades peaked before their worst tick.\n")
    lines.append("## Recommended R1.5 sweep ranges (data-derived only — Cooper selects)\n")
    lines.append("Lowest candidate level where false-stop rate (fires on a winner) first drops below 25%:")
    lines.append(f"- **Option 1 (scanner-anchored):** {lvl_s(o1)}")
    lines.append(f"- **Option 2 (gap-retracement):** {lvl_s(o2)}")
    lines.append("\nFull per-level false/true-stop and simulated-PF table: `stop_simulation.json` / Chart 5.\n")
    lines.append("## Edge cases / exclusions\n")
    if t1c:
        lines.append(f"- T1c gap_move≤0 (excluded from gap-fraction metrics): {t1c}")
    else:
        lines.append("- T1c: none — all 46 trades have gap_move > 0; all gap-fraction metrics valid.")
    zt = [(m["ticker"], m["date"]) for m in metrics if m.get("edge_case_zero_ticks")]
    if zt:
        lines.append(f"- T2a zero-tick hold windows (partial metrics): {zt}")
    else:
        lines.append("- T2a: none — every hold window contained ticks.")
    with open(out / "summary.md", "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def build_all(metrics, series, stops, n_wins, n_losses, t1c, charts, out,
              actual_pf, actual_cvar5, opt2_x):
    _scatter(metrics, "mae_gap_fraction", "mfe_gap_fraction",
             "MAE (gap fraction)", "MFE (gap fraction)",
             "MFE vs MAE — Gap Fraction Space | p=0.65 arm, 46 trades",
             charts / CHART_DEFS[0][0])
    _scatter(metrics, "mae_pct", "mfe_pct", "MAE (raw %)", "MFE (raw %)",
             "MFE vs MAE — Raw % Space | p=0.65 arm, 46 trades",
             charts / CHART_DEFS[1][0])
    _hist_mae(metrics, stops, opt2_x, charts / CHART_DEFS[2][0])
    _hist_mfe(metrics, charts / CHART_DEFS[3][0])
    _table(stops, charts / CHART_DEFS[4][0], actual_pf, actual_cvar5)
    _spaghetti(series, charts / CHART_DEFS[5][0])
    _bands(series, charts / CHART_DEFS[6][0])
    _timing(metrics, charts / CHART_DEFS[7][0])
    _index(charts / "index.html")
    _summary(metrics, stops, n_wins, n_losses, t1c, out, actual_pf, actual_cvar5)
    print(f"Charts + index + summary written to {charts} and {out}")
