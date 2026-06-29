"""
Phase DIAG-ENTRY T5 — timeline charts for the selected entry-failure events.

4 panels, x = seconds from session start (4am ET) to scanner_hit+600s:
  1. Price (30s OHLC) + entry/exit (TRADED only)
  2. lambda_hat(t) (buy+sell Hawkes intensity) vs EventAnchor threshold (k*lambda_ref)
  3. Gate state colored bands + lambda_v/peak ratio (right axis)
  4. Rolling 60s / 300s trade counts
Vertical markers (labels on Panel 1 only): 4am, EventAnchor, Warmup end, Scanner Hit,
Entry Deadline(+300s), Entry, Exit.

Run: python -m backtest.scripts.diag_entry_charts   (from repo root)
"""
from __future__ import annotations
import json
import math
import sys
from pathlib import Path

import numpy as np

BACKTEST = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKTEST))
sys.path.insert(0, str(BACKTEST.parent))

from data.loaders.trades import load_trades, _session_ns_bounds, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate
from runner_rapid import (
    _hawkes_replay_with_refit, _build_halt_intervals,
    EPG_K, EPG_TAU, EPG_WARMUP, HALT_GAP_THRESHOLD,
)
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND

R3 = BACKTEST / "results" / "phase_r3"
OUT = BACKTEST / "results" / "phase_diag_entry"
CHARTS = OUT / "charts"
CHARTS.mkdir(parents=True, exist_ok=True)
SAMPLE = BACKTEST / "data" / "val_r3_stratified.json"
P_OPEN = P_CLOSE = 0.65; TAU_PEAK = 600.0; GATE_C = 1.5; GATE_MODE = "peak"

_STATE_FILL = {"INACTIVE": "rgba(135,206,250,0.40)", "WARMUP": "rgba(255,255,0,0.40)",
               "PASS": "rgba(0,200,0,0.40)", "FAIL": "rgba(255,0,0,0.40)"}


def _hhmmss(sss):
    # sss = seconds from 4:00am ET
    tot = int(round(sss)); h = 4 + tot // 3600; m = (tot % 3600) // 60; s = tot % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


def _intervals(x, states):
    if len(x) == 0:
        return []
    out = []; cur = states[0]; s0 = x[0]
    for i in range(1, len(x)):
        if states[i] != cur:
            out.append((s0, x[i], cur)); cur = states[i]; s0 = x[i]
    out.append((s0, x[-1], cur)); return out


def replay(task):
    tk, dt = task["ticker"], task["date"]
    td = load_trades(tk, dt, task["mom_pct"])
    qd = load_quotes(tk, dt, task["mom_pct"])
    pc = get_prev_close(tk, dt)
    N = td.n_trades
    ts_ns = td.timestamps.astype(np.int64)
    start_ns, _ = _session_ns_bounds(dt)
    sss = (ts_ns - start_ns) / NS_PER_SECOND          # seconds from 4am
    scanner_sss = (int(task["scanner_hit_ts_ns"]) - start_ns) / NS_PER_SECOND
    fp = task["fp"]
    tier = task["q_bar_cfg"].get("wide", {}).get("median", 250.0)
    ofi = compute_trade_ofi(
        trade_timestamps=td.timestamps, trade_prices=td.prices,
        trade_sizes=td.sizes.astype(np.float64), quote_timestamps=qd.timestamps,
        quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
        quote_bid_sizes=qd.bid_sizes.astype(np.float64),
        quote_ask_sizes=qd.ask_sizes.astype(np.float64), window_sec=10.0, q_bar_fallback=tier)
    sides = ofi.sides
    halts = _build_halt_intervals(td)
    lb = np.zeros(N); ls = np.zeros(N); E = np.zeros(N); Ed = np.zeros(N); nb = np.zeros(N)
    dv = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
    glref = fp["mu_buy"] + fp["mu_sell"]
    plref = compute_lambda_ref_per_event(tk, dt)
    lref = glref if (math.isnan(plref) or plref <= 0) else plref
    cold = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=sides, rho=task["rho"], lambda_ref=lref, init_params=fp,
        rho_E=task["rho"], lam_buy_out=lb, lam_sell_out=ls, E_out=E, Edot_out=Ed,
        n_base_out=nb, dv_arr=dv, halt_intervals=halts or None)
    lambda_hat = lb + ls
    lref_eff = glref
    if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
        lref_eff = cold.mu_buy + cold.mu_sell
    anchor = EventAnchor(lambda_ref=glref, k_multiplier=EPG_K)
    if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
        anchor.set_lambda_ref(cold.mu_buy + cold.mu_sell)
    gate = ParticipationGate(half_life_seconds=EPG_TAU, peak_threshold_p=P_OPEN,
                             warmup_seconds=EPG_WARMUP, gate_mode=GATE_MODE,
                             tau_peak=TAU_PEAK, C=GATE_C, p_open=P_OPEN, p_close=P_CLOSE)
    states = []; ratio = np.full(N, np.nan); anchor_sss = None
    for i in range(N):
        ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if ev is not None and anchor_sss is None:
            anchor_sss = float(sss[i])
            gate.activate(ev)              # MUST activate gate on anchor fire (matches runner)
        gt = td.t_sec[i]
        if i > 0 and halts and td.t_sec[i] - td.t_sec[i-1] > HALT_GAP_THRESHOLD:
            for hs, he in halts:
                if td.t_sec[i-1] < he and td.t_sec[i] > hs:
                    gt = td.t_sec[i-1] + 1e-6; break
        states.append(gate.update(float(td.prices[i]) * float(td.sizes[i]), gt).name)
        if gate._lambda_v_peak > 0:
            ratio[i] = gate._lambda_v / gate._lambda_v_peak
    return dict(sss=sss, prices=td.prices.astype(float), lambda_hat=lambda_hat,
                states=np.array(states), ratio=ratio, scanner_sss=scanner_sss,
                anchor_sss=anchor_sss, threshold=float(EPG_K * lref_eff), N=N)


def render(task, rep, audit_rec, trade, out_dir):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    tk, dt = task["ticker"], task["date"]
    lo, hi = 0.0, rep["scanner_sss"] + 600.0
    sss = rep["sss"]; m = (sss >= lo) & (sss <= hi)
    xw = sss[m]; nW = len(xw); step = max(1, nW // 15000) if nW > 30000 else 1

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04,
                        row_heights=[0.28, 0.25, 0.25, 0.22],
                        specs=[[{}], [{}], [{"secondary_y": True}], [{}]],
                        subplot_titles=["Price (30s OHLC)",
                                        "λ_hat (buy+sell intensity) vs Anchor threshold",
                                        "Gate state + λ_v/peak ratio",
                                        "Rolling trade count (60s / 300s)"])

    # Panel 1: 30s OHLC
    pw = rep["prices"][m]
    if len(xw):
        bins = np.floor(xw / 30.0).astype(int)
        for b in np.unique(bins):
            bp = pw[bins == b]; x0 = b * 30.0
            fig.add_trace(go.Candlestick(x=[x0 + 15], open=[bp[0]], high=[bp.max()],
                          low=[bp.min()], close=[bp[-1]], increasing_line_color="#2ca02c",
                          decreasing_line_color="#d62728", showlegend=False), row=1, col=1)
    if trade is not None:
        off = rep["scanner_sss"] - audit_rec["scanner_hit_t_sec"]  # sss = t_sec_frame + off
        e_sss = trade["entry_t_sec"] + off; x_sss = trade["exit_t_sec"] + off
        fig.add_trace(go.Scatter(x=[e_sss], y=[trade["entry_price"]], mode="markers",
                      marker=dict(symbol="triangle-up", size=14, color="green"), name="Entry"),
                      row=1, col=1)
        ec = "green" if trade["pnl_pct"] > 0 else "red"
        fig.add_trace(go.Scatter(x=[x_sss], y=[trade["exit_price"]], mode="markers",
                      marker=dict(symbol="triangle-down", size=14, color=ec),
                      name=f"Exit ({trade['exit_reason']})"), row=1, col=1)

    # Panel 2: lambda_hat + threshold
    fig.add_trace(go.Scatter(x=xw[::step], y=rep["lambda_hat"][m][::step], mode="lines",
                  line=dict(color="#1f77b4", width=1.2), name="λ_hat"), row=2, col=1)
    fig.add_hline(y=rep["threshold"], line=dict(color="red", dash="dash", width=1.3),
                  annotation_text=f"Anchor threshold (k×λ_ref={rep['threshold']:.3g})",
                  annotation_position="top right", row=2, col=1)

    # Panel 3: state bands + ratio
    iv = [(max(a, lo), min(b, hi), s) for (a, b, s) in _intervals(sss.tolist(), rep["states"])
          if not (b < lo or a > hi)]
    for a, b, s in iv:
        fig.add_shape(type="rect", x0=a, x1=b, y0=0, y1=1, xref="x", yref="y domain",
                      row=3, col=1, fillcolor=_STATE_FILL.get(s, "rgba(200,200,200,0.3)"),
                      line_width=0, layer="below")
    for s, c in _STATE_FILL.items():
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                      marker=dict(size=10, color=c, symbol="square"), name=s), row=3, col=1)
    fig.add_trace(go.Scatter(x=xw[::step], y=rep["ratio"][m][::step], mode="lines",
                  line=dict(color="gray", width=1.0), name="λ_v/peak"),
                  row=3, col=1, secondary_y=True)

    # Panel 4: rolling counts
    grid = np.arange(lo, hi + 1, 10.0)
    allx = sss
    c60 = np.searchsorted(allx, grid) - np.searchsorted(allx, grid - 60.0)
    c300 = np.searchsorted(allx, grid) - np.searchsorted(allx, grid - 300.0)
    fig.add_trace(go.Scatter(x=grid, y=c60, mode="lines", line=dict(color="purple", width=1.6),
                  name="trades/60s"), row=4, col=1)
    fig.add_trace(go.Scatter(x=grid, y=c300, mode="lines",
                  line=dict(color="purple", width=1.0, dash="dash"), name="trades/300s"),
                  row=4, col=1)

    # markers
    marks = [(0.0, "gray", "4am ET"),
             (rep["scanner_sss"], "blue", f"Scanner Hit {_hhmmss(rep['scanner_sss'])}"),
             (rep["scanner_sss"] + 300.0, "orange", "Entry Deadline (+300s)")]
    if rep["anchor_sss"] is not None:
        marks.append((rep["anchor_sss"], "purple", f"EventAnchor {_hhmmss(rep['anchor_sss'])}"))
        marks.append((rep["anchor_sss"] + EPG_WARMUP, "gray", "Warmup end"))
    if trade is not None:
        off = rep["scanner_sss"] - audit_rec["scanner_hit_t_sec"]
        marks.append((trade["entry_t_sec"] + off, "green", "Entry"))
        marks.append((trade["exit_t_sec"] + off, "red", f"Exit ({trade['exit_reason']})"))
    for x, color, label in marks:
        if not (lo <= x <= hi):
            continue
        for row in (1, 2, 3, 4):
            fig.add_vline(x=x, line=dict(color=color, dash="dash", width=1.2),
                          row=row, col=1,
                          **({"annotation_text": label, "annotation_position": "top",
                              "annotation_font_size": 9} if row == 1 else {}))

    fr = audit_rec["entry_failure_reason"]; strat = audit_rec["stratum"]
    npre = audit_rec["n_trades_before_scanner"]; fa = audit_rec["is_first_appearance"]
    fig.update_layout(
        title=dict(text=f"{tk} {dt} | {fr} | stratum={strat} | n_pre={npre} | 1st_appear={fa}",
                   font=dict(size=14)),
        height=1100, width=1500, hovermode="x unified", xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(255,255,255,0.6)"))
    fig.update_xaxes(range=[lo, hi])
    fig.update_xaxes(title_text="seconds from 4am ET (markers labeled with ET wall-clock)", row=4, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="λ_hat", row=2, col=1)
    fig.update_yaxes(title_text="Gate state", range=[0, 1], showticklabels=False, row=3, col=1)
    fig.update_yaxes(title_text="λ_v/peak", range=[0, 1.1], row=3, col=1, secondary_y=True)
    fig.update_yaxes(title_text="Trade count", row=4, col=1)
    fig.write_html(str(out_dir / f"{tk}_{dt}.html"), include_plotlyjs="cdn", config={"responsive": True})


def build_index(rows, out_dir):
    body = "\n".join(
        f'<tr><td>{r["chart_category"]}</td><td>{r["ticker"]}</td><td>{r["date"]}</td>'
        f'<td>{r["stratum"]}</td><td>{r["failure_reason"]}</td><td>{r["is_first_appearance"]}</td>'
        f'<td>{r["n_trades_before_scanner"]}</td>'
        f'<td><a href="{r["ticker"]}_{r["date"]}.html" target="_blank">chart</a></td></tr>'
        for r in rows)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8"><title>DIAG-ENTRY timeline charts</title>
<style>body{{font-family:monospace;font-size:13px;padding:16px}}
table{{border-collapse:collapse;width:100%;max-width:1100px}}
th,td{{border:1px solid #ccc;padding:5px 10px;text-align:left}}
th{{background:#37474f;color:#fff}} tr:hover{{background:#f5f5f5}} a{{color:#1565c0;text-decoration:none}}
</style></head><body>
<h2>Phase DIAG-ENTRY — entry-failure timeline charts ({len(rows)} events)</h2>
<table><thead><tr><th>Category</th><th>Ticker</th><th>Date</th><th>Stratum</th>
<th>Failure reason</th><th>1st appear</th><th>N pre-scanner</th><th>Link</th></tr></thead>
<tbody>{body}</tbody></table></body></html>"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    sel = json.load(open(OUT / "chart_selection.json"))
    audit = {(a["ticker"], a["date"]): a for a in json.load(open(OUT / "entry_audit.json"))}
    trades = {(t["ticker"], t["date"]): t for t in json.load(open(R3 / "disabled" / "per_trade.json"))}
    ev = {(e["ticker"], e["date"]): e for e in json.load(open(SAMPLE))["events"]}
    hm = json.load(open(CONFIG_DIR / "hawkes_params.json"))
    qcfg = json.load(open(CONFIG_DIR / "q_bar_tiers.json"))
    pep = {}
    pa = BACKTEST.parent / "results" / "phase_a" / "production_fit_results.json"
    if pa.exists():
        for r in json.load(open(pa)):
            if r.get("status") == "success" and "final_params" in r:
                pep[(r["ticker"], r["date"])] = r["final_params"]
    rho = hm.get("rho", 0.99)

    t5b_flags = []
    n_ok = 0
    for s in sel:
        k = (s["ticker"], s["date"]); meta = ev[k]
        task = {"ticker": s["ticker"], "date": s["date"], "mom_pct": meta["mom_pct"],
                "scanner_hit_ts_ns": meta["scanner_hit_ts_ns"], "fp": pep.get(k, hm),
                "rho": rho, "q_bar_cfg": qcfg}
        try:
            rep = replay(task)
        except Exception as e:
            print(f"  replay FAIL {k}: {e}"); continue
        # T5b: ANCHOR_NEVER_FIRED -> lambda_hat must stay below threshold in window
        if s["failure_reason"] == "ANCHOR_NEVER_FIRED":
            win = (rep["sss"] >= 0) & (rep["sss"] <= rep["scanner_sss"] + 300)
            mx = float(np.nanmax(rep["lambda_hat"][win])) if win.any() else 0.0
            if mx >= rep["threshold"]:
                t5b_flags.append((k, mx, rep["threshold"]))
        render(task, rep, audit[k], trades.get(k), CHARTS)
        n_ok += 1
        print(f"  OK {k}  {s['failure_reason']}")
    build_index(sel, CHARTS)
    print(f"\n{n_ok}/{len(sel)} charts + index written to {CHARTS}")
    if t5b_flags:
        print(f"!! T5b DISCREPANCY (lambda_hat exceeded threshold but anchor never fired): {t5b_flags}")
    else:
        print("T5b: no discrepancies (or no ANCHOR_NEVER_FIRED events selected)")


if __name__ == "__main__":
    main()
