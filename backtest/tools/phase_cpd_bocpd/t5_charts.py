"""
Phase CPD-BOCPD-VIZ — Tasks T3 + T4
=====================================
Per-event 5-panel diagnostic charts for the BOCPD winner config.
Reads from replay_cache.json produced by bocpd_replay.py.

Panel layout (shared x = active seconds since T_event; dark theme):
  1 Price       10s candles + entry (green ▲ win / red ▲ loss) + exit (▼)
  2 WJI raw     wji_raw line + horizontal bg ref at 1.0
  3 WJI_log     log(WJI) line + 0-ref + +/-sigma dashed bands + PASS shading
  4 P_regime    line + p_enter solid / p_exit dashed + PASS shading
  5 Dominant RL argmax(R) step trace — drops to ~0 on changepoint, rises in regime

NOTE: Panel 3 substituted for standard buy-intensity panel — documented in spec.

Outputs:
  results/phase_cpd_bocpd/viz/event_charts/{TICKER}_{DATE}.html
  results/phase_cpd_bocpd/viz/event_charts/index.html

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd_bocpd.t5_charts
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

REPLAY_CACHE = REPO_ROOT / "results" / "phase_cpd_bocpd" / "viz" / "replay_cache.json"
OUT_DIR      = REPO_ROOT / "results" / "phase_cpd_bocpd" / "viz" / "event_charts"

WINNER_LAMBDA_H = 0.01
WINNER_P_ENTER  = 0.60
WINNER_P_EXIT   = 0.50
WARMUP_SEC      = 300.0
PLOT_MAX_POINTS = 5000

BG_COLOR    = "#1a1a2e"
PAPER_COLOR = "#16213e"
GRID_COLOR  = "#2d2d4e"
TEXT_COLOR  = "#e0e0e0"
PASS_FILL   = "rgba(0,200,80,0.12)"
WARMUP_FILL = "rgba(255,167,38,0.12)"


# ══════════════════════════════════════════════════════════════════════
#  Helpers
# ══════════════════════════════════════════════════════════════════════

def _downsample(x, *ys, max_pts=PLOT_MAX_POINTS):
    n = len(x)
    if n <= max_pts:
        return (x, *ys)
    step = int(math.ceil(n / max_pts))
    idx = list(range(0, n, step))
    if idx[-1] != n - 1:
        idx.append(n - 1)
    idx = np.array(idx)
    return (x[idx], *[y[idx] for y in ys])


def _pass_intervals(tv, gate_states):
    intervals = []; in_pass = False; start = None
    for i, gs in enumerate(gate_states):
        if gs == "PASS" and not in_pass:
            in_pass = True; start = float(tv[i])
        elif gs != "PASS" and in_pass:
            in_pass = False; intervals.append((start, float(tv[i])))
    if in_pass:
        intervals.append((start, float(tv[-1])))
    return intervals


def _cvar5_event(pnls):
    if len(pnls) < 5:
        return None
    arr = sorted(pnls)
    n5 = max(1, math.floor(len(arr) * 0.05))
    return float(sum(arr[:n5]) / n5)


# ══════════════════════════════════════════════════════════════════════
#  Chart builder
# ══════════════════════════════════════════════════════════════════════

def build_chart(result: dict, out_path: Path) -> None:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio

    ticker = result["ticker"]; date = result["date"]
    ticks  = result["ticks"]; trades = result["trades"]
    ohlcv  = result["ohlcv_10s"]
    sigma_log = float(result.get("sigma_log") or 0.209)
    t_event_ns = result.get("t_event_ns")
    n_trades = result["n_trades"]
    event_pf = result.get("event_pf")
    pf_str = f"{event_pf:.3f}" if event_pf is not None and math.isfinite(event_pf) else "inf"

    if not ticks:
        return

    all_ts   = np.array([t["ts_ns"]    for t in ticks], dtype=np.int64)
    all_ta   = np.array([t["t_active"] for t in ticks], dtype=np.float64)
    all_wraw = np.array([t["wji_raw"]  for t in ticks], dtype=np.float64)
    all_wlog = np.array([
        t["wji_log"] if t["wji_log"] is not None else float("nan")
        for t in ticks], dtype=np.float64)
    all_gs   = [t["gate_state"] for t in ticks]
    all_pr   = np.array([
        t["p_regime"] if t["p_regime"] is not None else float("nan")
        for t in ticks], dtype=np.float64)
    all_rl   = np.array([
        t["dominant_rl"] if t["dominant_rl"] is not None else float("nan")
        for t in ticks], dtype=np.float64)

    if t_event_ns is not None:
        ev_pos = int(np.searchsorted(all_ts, t_event_ns, side="left"))
        ev_pos = min(ev_pos, len(all_ta) - 1)
        t_event_active = float(all_ta[ev_pos])
    else:
        t_event_active = float(all_ta[0])

    tse = all_ta - t_event_active
    post = tse >= 0.0
    tv   = tse[post]; wraw = all_wraw[post]; wlog = all_wlog[post]
    pr   = all_pr[post]; rl = all_rl[post]
    gs   = [all_gs[i] for i in range(len(all_gs)) if post[i]]
    pass_iv = _pass_intervals(tv, gs)

    # OHLCV: map bar ts_ns to tse
    candle_tse = []; co = []; ch = []; clo = []; cc = []
    if ohlcv and t_event_ns is not None:
        for bar in ohlcv:
            btse = (bar["open_ts_ns"] - t_event_ns) / 1e9
            if btse < 0:
                continue
            candle_tse.append(btse); co.append(bar["open"])
            ch.append(bar["high"]); clo.append(bar["low"]); cc.append(bar["close"])

    # Trade markers
    e_win_t, e_win_p, e_loss_t, e_loss_p = [], [], [], []
    x_win_t, x_win_p, x_loss_t, x_loss_p = [], [], [], []
    for tr in trades:
        ets = (tr["entry_ts_ns"] - t_event_ns) / 1e9 if t_event_ns else 0.0
        xts = (tr["exit_ts_ns"]  - t_event_ns) / 1e9 if t_event_ns else 0.0
        win = tr["pnl_pct"] > 0
        if win:
            e_win_t.append(ets);  e_win_p.append(tr["entry_price"])
            x_win_t.append(xts);  x_win_p.append(tr["exit_price"])
        else:
            e_loss_t.append(ets); e_loss_p.append(tr["entry_price"])
            x_loss_t.append(xts); x_loss_p.append(tr["exit_price"])

    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.028,
        row_heights=[4, 2, 2, 2, 1.5],
        subplot_titles=(
            "Price (10s candles)",
            "WJI raw  (background reference = 1.0)",
            "WJI_log = log(WJI)  [BOCPD input; replaces buy-intensity per VIZ spec]",
            f"P_regime  (p_enter={WINNER_P_ENTER:.2f}  p_exit={WINNER_P_EXIT:.2f})",
            "Dominant run-length  argmax(R)  [drops to 0 at changepoints]",
        ),
    )

    # Panel 1
    if candle_tse:
        fig.add_trace(go.Candlestick(
            x=candle_tse, open=co, high=ch, low=clo, close=cc,
            increasing_line_color="#26a69a", decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a", decreasing_fillcolor="#ef5350",
            name="price", showlegend=False,
        ), row=1, col=1)
    if e_win_t:
        fig.add_trace(go.Scatter(x=e_win_t, y=e_win_p, mode="markers",
            marker=dict(symbol="triangle-up", color="#00e676", size=10), showlegend=False), row=1, col=1)
    if e_loss_t:
        fig.add_trace(go.Scatter(x=e_loss_t, y=e_loss_p, mode="markers",
            marker=dict(symbol="triangle-up", color="#ff1744", size=10), showlegend=False), row=1, col=1)
    if x_win_t:
        fig.add_trace(go.Scatter(x=x_win_t, y=x_win_p, mode="markers",
            marker=dict(symbol="triangle-down", color="#00e676", size=10), showlegend=False), row=1, col=1)
    if x_loss_t:
        fig.add_trace(go.Scatter(x=x_loss_t, y=x_loss_p, mode="markers",
            marker=dict(symbol="triangle-down", color="#ff1744", size=10), showlegend=False), row=1, col=1)

    # Panels 2-5 downsampled
    dt, dw, dwl, dpr, drl = _downsample(tv, wraw, wlog, pr, rl)

    fig.add_trace(go.Scatter(x=dt, y=dw, mode="lines",
        line=dict(color="#64b5f6", width=1), showlegend=False), row=2, col=1)
    fig.add_hline(y=1.0, line=dict(color="#546e7a", width=1, dash="dash"),
        annotation_text="bg=1.0", annotation_font_color=TEXT_COLOR, row=2, col=1)

    fig.add_trace(go.Scatter(x=dt, y=dwl, mode="lines",
        line=dict(color="#81c784", width=1), showlegend=False), row=3, col=1)
    fig.add_hline(y=0.0, line=dict(color="#546e7a", width=1, dash="dash"),
        annotation_text="0", annotation_font_color=TEXT_COLOR, row=3, col=1)
    fig.add_hline(y=sigma_log,  line=dict(color="#ffb74d", width=1, dash="dot"),
        annotation_text=f"+σ={sigma_log:.3f}", annotation_font_color="#ffb74d", row=3, col=1)
    fig.add_hline(y=-sigma_log, line=dict(color="#ffb74d", width=1, dash="dot"),
        annotation_text=f"-σ={-sigma_log:.3f}", annotation_font_color="#ffb74d", row=3, col=1)

    valid_pr = ~np.isnan(dpr)
    if valid_pr.any():
        fig.add_trace(go.Scatter(x=dt[valid_pr], y=dpr[valid_pr], mode="lines",
            line=dict(color="#ce93d8", width=1.2), showlegend=False), row=4, col=1)
    fig.add_hline(y=WINNER_P_ENTER, line=dict(color="#ef5350", width=1.2),
        annotation_text=f"p_enter={WINNER_P_ENTER:.2f}", annotation_font_color="#ef5350", row=4, col=1)
    fig.add_hline(y=WINNER_P_EXIT, line=dict(color="#ff8a65", width=1, dash="dash"),
        annotation_text=f"p_exit={WINNER_P_EXIT:.2f}", annotation_font_color="#ff8a65", row=4, col=1)
    fig.update_yaxes(range=[0, 1], row=4, col=1)

    valid_rl = ~np.isnan(drl)
    if valid_rl.any():
        fig.add_trace(go.Scatter(x=dt[valid_rl], y=drl[valid_rl], mode="lines",
            line=dict(color="#80cbc4", width=1, shape="hv"), showlegend=False), row=5, col=1)

    for row in range(1, 6):
        fig.add_vrect(x0=0.0, x1=WARMUP_SEC, fillcolor=WARMUP_FILL, line_width=0, row=row, col=1)
        for a, b in pass_iv:
            fig.add_vrect(x0=a, x1=b, fillcolor=PASS_FILL, line_width=0, row=row, col=1)
    fig.add_vline(x=0.0, line=dict(color="#b0bec5", width=1, dash="dash"), row=1, col=1)

    axis_style = dict(gridcolor=GRID_COLOR, zeroline=False,
                      tickfont=dict(color=TEXT_COLOR), title_font=dict(color=TEXT_COLOR))
    for i in range(1, 6):
        fig.update_xaxes(**axis_style, row=i, col=1, rangeslider_visible=False)
        fig.update_yaxes(**axis_style, row=i, col=1)
    fig.update_xaxes(title_text="active seconds since T_event", row=5, col=1)

    fig.update_layout(
        height=1100, paper_bgcolor=PAPER_COLOR, plot_bgcolor=BG_COLOR,
        font=dict(color=TEXT_COLOR, size=11), showlegend=False,
        margin=dict(l=60, r=30, t=60, b=40),
        title=dict(
            text=(f"{ticker} {date} | BOCPD lh={WINNER_LAMBDA_H} pe={WINNER_P_ENTER:.2f} "
                  f"| PF={pf_str} n={n_trades}"),
            font=dict(color=TEXT_COLOR, size=14),
        ),
    )
    for ann in fig.layout.annotations:
        ann.font.color = TEXT_COLOR

    out_path.parent.mkdir(parents=True, exist_ok=True)
    pio.write_html(fig, str(out_path), include_plotlyjs=True, auto_open=False)


# ══════════════════════════════════════════════════════════════════════
#  Index builder
# ══════════════════════════════════════════════════════════════════════

def write_index(rows: list[dict], path: Path) -> None:
    cols = [("ticker","Ticker"),("date","Date"),("n_trades","n_trades"),
            ("event_pf","event_PF"),("cvar5_event","CVaR5_event"),
            ("worst_trade","worst_trade%"),("link","chart")]
    head = "".join(f'<th onclick="sortBy({i})">{lbl}</th>' for i,(_, lbl) in enumerate(cols))
    trs = []
    for r in rows:
        pf  = f"{r['event_pf']:.3f}" if r.get("event_pf") is not None and math.isfinite(r.get("event_pf", float("nan"))) else "inf"
        cv  = f"{r['cvar5_event']:.2f}" if r.get("cvar5_event") is not None else "N/A"
        wt  = f"{r['worst_trade']:.2f}" if r.get("worst_trade") is not None else "N/A"
        fn  = f"{r['ticker']}_{r['date']}.html"
        cells = [f"<td>{r['ticker']}</td>", f"<td>{r['date']}</td>",
                 f"<td>{r['n_trades']}</td>", f"<td>{pf}</td>", f"<td>{cv}</td>",
                 f"<td>{wt}</td>", f"<td><a href='./{fn}'>chart</a></td>"]
        trs.append("<tr>" + "".join(cells) + "</tr>")
    html = f"""<!doctype html><html><head><meta charset='utf-8'>
<title>CPD-BOCPD lh=0.01 pe=0.60 - event charts</title>
<style>body{{font-family:sans-serif;background:#1a1a2e;color:#e0e0e0;margin:24px}}
h2{{color:#ce93d8}}p{{color:#b0bec5;font-size:13px}}
table{{border-collapse:collapse;width:100%}}
th,td{{border:1px solid #2d2d4e;padding:7px 12px;text-align:right}}
th{{cursor:pointer;background:#16213e;color:#80cbc4}}th:hover{{background:#0f3460}}
td:first-child,th:first-child{{text-align:left}}td:last-child{{text-align:center}}
tr:nth-child(even){{background:#16213e}}tr:nth-child(odd){{background:#1a1a2e}}
tr:hover{{background:#0f3460}}a{{color:#64b5f6;text-decoration:none}}
a:hover{{text-decoration:underline}}</style></head>
<body>
<h2>CPD-BOCPD — Winner config: lambda_h=0.01, p_enter=0.60, p_exit=0.50</h2>
<p>{len(rows)} events with trades &middot; click header to sort &middot; default: event_PF desc</p>
<table id='t'><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>
<script>
var _asc={{}};
function sortBy(c){{
  var tb=document.querySelector('#t tbody'),rs=[...tb.rows];
  var a=_asc[c]===undefined?false:!_asc[c];_asc[c]=a;
  rs.sort(function(x,y){{
    var av=x.cells[c].innerText.trim(),bv=y.cells[c].innerText.trim();
    var na=parseFloat(av),nb=parseFloat(bv);
    if(!isNaN(na)&&!isNaN(nb))return a?na-nb:nb-na;
    return a?av.localeCompare(bv):bv.localeCompare(av);
  }});rs.forEach(function(r){{tb.appendChild(r);}});
}}
sortBy(3);
</script></body></html>"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
#  Main
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    if not REPLAY_CACHE.exists():
        print(f"*** replay_cache.json not found at {REPLAY_CACHE} — run bocpd_replay.py first. ***")
        sys.exit(1)

    import time
    t0 = time.time()
    cache = json.load(REPLAY_CACHE.open(encoding="utf-8"))
    results = cache.get("events", {})

    traded = [r for r in results.values() if r.get("status") == "ok" and r.get("n_trades", 0) > 0]
    errs   = [r for r in results.values() if r.get("status") == "error"]
    no_t   = [r for r in results.values() if r.get("status") == "ok" and r.get("n_trades", 0) == 0]
    n_all  = len(results)

    print(f"T3 charts — {n_all} events in cache: {len(traded)} traded, "
          f"{len(no_t)} no-trades, {len(errs)} errors")

    if errs:
        err_rate = len(errs) / max(n_all, 1)
        if err_rate > 0.05:
            print(f"*** HARD STOP: error rate {err_rate:.1%} > 5%. "
                  f"First error: {errs[0].get('error', '?')} ***")
            sys.exit(1)
        print(f"  {len(errs)} errors (rate {err_rate:.1%} — below 5% threshold, continuing)")

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    chart_ok = 0; chart_err = 0
    index_rows = []
    for r in sorted(traded, key=lambda x: (x["ticker"], x["date"])):
        out_path = OUT_DIR / f"{r['ticker']}_{r['date']}.html"
        try:
            build_chart(r, out_path)
            pnls = [t["pnl_pct"] for t in r.get("trades", [])]
            index_rows.append({
                "ticker":       r["ticker"],
                "date":         r["date"],
                "n_trades":     r["n_trades"],
                "event_pf":     r.get("event_pf"),
                "cvar5_event":  _cvar5_event(pnls),
                "worst_trade":  min(pnls) if pnls else None,
            })
            chart_ok += 1
        except Exception as exc:
            chart_err += 1
            print(f"  chart error {r['ticker']} {r['date']}: {exc}")

    chart_err_rate = chart_err / max(len(traded), 1)
    if chart_err_rate > 0.05:
        print(f"*** HARD STOP: chart error rate {chart_err_rate:.1%} > 5% ***")
        sys.exit(1)

    write_index(index_rows, OUT_DIR / "index.html")
    print(f"\nT4 index written. {chart_ok} charts in {time.time()-t0:.0f}s, "
          f"{chart_err} chart errors.")
    print(f"→ {OUT_DIR}")


if __name__ == "__main__":
    main()
