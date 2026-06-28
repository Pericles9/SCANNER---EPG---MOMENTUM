#!/usr/bin/env python3
"""Phase R3 T6 — per-event 4-panel charts for the ROC-disabled arm (val_r3, p=0.65, T_gate=500).

Same engine as r15_charts but: entry label + title carry roc_5m / gap; index adds
stratum, roc_5m (nulls last), is_first_appearance. Run: python -m backtest.r3_charts
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from backtest.runner_rapid import (
    _hawkes_replay_with_refit, _build_halt_intervals,
    EPG_K, EPG_TAU, EPG_WARMUP, HALT_GAP_THRESHOLD,
)
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from data.loaders.trades import load_trades, _session_ns_bounds, compute_lambda_ref_per_event
from data.loaders.quotes import load_quotes
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate

BACKTEST = Path(__file__).resolve().parent
REPO = BACKTEST.parent
ARM_DIR = BACKTEST / "results" / "phase_r3" / "disabled"
CHART_DIR = BACKTEST / "results" / "phase_r3" / "event_charts" / "disabled"
SAMPLE = BACKTEST / "data" / "val_r3_stratified.json"
ROC_VALUES = BACKTEST / "results" / "phase_r3" / "roc_values.json"

P_OPEN = P_CLOSE = 0.65
GATE_MODE = "peak"; TAU_PEAK = 600.0; GATE_C = 1.5
THETA = 0.65; T_GATE = 500.0
WIN_PRE = 600.0; WIN_POST = 120.0

CHART_DIR.mkdir(parents=True, exist_ok=True)
_STATE = {"INACTIVE": "rgba(190,190,190,0.85)", "WARMUP": "rgba(255,165,0,0.85)",
          "PASS": "rgba(60,180,60,0.85)", "FAIL": "rgba(210,60,60,0.85)"}


def _bars30(ts, px, start_ns):
    BAR = 30 * NS_PER_SECOND
    if len(ts) == 0:
        e = np.array([]); return np.array([], dtype=np.int64), e, e, e, e
    bi = ((ts.astype(np.int64) - start_ns) // BAR).astype(np.int64)
    st, o, h, l, c = [], [], [], [], []
    for b in np.unique(bi):
        bp = px[bi == b]
        st.append(int(start_ns + b * BAR)); o.append(float(bp[0])); h.append(float(bp.max()))
        l.append(float(bp.min())); c.append(float(bp[-1]))
    return np.array(st, dtype=np.int64), np.array(o), np.array(h), np.array(l), np.array(c)


def _intervals(t, states):
    if not t:
        return []
    out = []; cur = states[0]; s0 = t[0]
    for i in range(1, len(t)):
        if states[i] != cur:
            out.append((s0, t[i], cur)); cur = states[i]; s0 = t[i]
    out.append((s0, t[-1], cur)); return out


def collect(task):
    tk = task["ticker"]; dt = task["date"]; trade = task["trade"]; rocm = task["roc"]
    fp = task["fp"]; rho = task["rho"]; qcfg = task["q_bar_cfg"]; sh = task["scanner_hit_ts_ns"]
    try:
        td = load_trades(tk, dt, task["mom_pct"])
        if td is None or td.n_trades < 30:
            return {"status": "skip", "ticker": tk, "date": dt, "reason": "trades"}
        qd = load_quotes(tk, dt, task["mom_pct"])
        if qd is None or qd.n_quotes < 10:
            return {"status": "skip", "ticker": tk, "date": dt, "reason": "quotes"}
        pc = get_prev_close(tk, dt)
        if pc is None or pc <= 0:
            return {"status": "skip", "ticker": tk, "date": dt, "reason": "prev_close"}
        N = td.n_trades; start_ns, _ = _session_ns_bounds(dt)
        scanner_t = (int(sh) - int(td.timestamps[0])) / NS_PER_SECOND if sh is not None else None
        tier = qcfg.get("wide", {}).get("median", 250.0)
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
        glref = fp["mu_buy"] + fp["mu_sell"]; plref = compute_lambda_ref_per_event(tk, dt)
        lref = glref if (math.isnan(plref) or plref <= 0) else plref
        cold = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=rho, lambda_ref=lref, init_params=fp, rho_E=rho,
            lam_buy_out=lb, lam_sell_out=ls, E_out=E, Edot_out=Ed, n_base_out=nb, dv_arr=dv,
            halt_intervals=halts or None)
        lam = lb + ls
        anchor = EventAnchor(lambda_ref=glref, k_multiplier=EPG_K)
        if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
            anchor.set_lambda_ref(cold.mu_buy + cold.mu_sell)
        gate = ParticipationGate(half_life_seconds=EPG_TAU, peak_threshold_p=P_OPEN,
                                 warmup_seconds=EPG_WARMUP, gate_mode=GATE_MODE,
                                 tau_peak=TAU_PEAK, C=GATE_C, p_open=P_OPEN, p_close=P_CLOSE)
        states = []; ratio = np.full(N, np.nan); fired = False
        for i in range(N):
            tev = anchor.update(lam[i], td.t_sec[i])
            if tev is not None and not fired:
                gate.activate(tev); fired = True
            gt = td.t_sec[i]
            if i > 0 and halts and td.t_sec[i] - td.t_sec[i-1] > HALT_GAP_THRESHOLD:
                for hs, he in halts:
                    if td.t_sec[i-1] < he and td.t_sec[i] > hs:
                        gt = td.t_sec[i-1] + 1e-6; break
            st = gate.update(float(td.prices[i]) * float(td.sizes[i]), gt)
            states.append(st.name)
            if gate._lambda_v_peak > 0:
                ratio[i] = gate._lambda_v / gate._lambda_v_peak
        if not fired:
            return {"status": "skip", "ticker": tk, "date": dt, "reason": "no_t_event"}
        Isell = np.where(lam > 0, ls / lam, np.nan)
        lo = max(float(td.t_sec[0]), trade["entry_t_sec"] - WIN_PRE)
        hi = min(float(td.t_sec[-1]), max(trade["exit_t_sec"] + WIN_POST, trade["entry_t_sec"] + T_GATE + 30.0))
        t = td.t_sec; m = (t >= lo) & (t <= hi); tw = t[m]; nW = len(tw)
        step = max(1, nW // 20000) if nW > 40000 else 1
        si = [(max(s0, lo), min(s1, hi), s) for (s0, s1, s) in _intervals(t.tolist(), states)
              if not (s1 < lo or s0 > hi)]
        bs, bo, bh, bl, bc = _bars30(td.timestamps, td.prices, start_ns)
        bt = (bs.astype(np.float64) - int(td.timestamps[0])) / NS_PER_SECOND
        bm = (bt >= lo) & (bt <= hi)
        return {"status": "ok", "ticker": tk, "date": dt, "trade": trade, "roc": rocm,
                "scanner_t": scanner_t, "win_lo": lo, "win_hi": hi,
                "tw": tw[::step].tolist(), "ratio": ratio[m][::step].tolist(),
                "isell": Isell[m][::step].tolist(), "state_intervals": si,
                "bt": (bt[bm] + 15.0).tolist(), "bo": bo[bm].tolist(), "bh": bh[bm].tolist(),
                "bl": bl[bm].tolist(), "bc": bc[bm].tolist()}
    except Exception as e:
        import traceback
        return {"status": "error", "ticker": tk, "date": dt, "error": str(e), "tb": traceback.format_exc()[-600:]}


def _roc_label(r):
    if r.get("is_first_appearance"):
        return "N/A (1st appear)"
    v = r.get("scanner_roc_5m_at_fire")
    if r.get("is_partial_window"):
        return f"{v:.3f} (partial {r.get('scanner_roc_window_sec_actual',0):.0f}s)"
    return f"{v:.3f}" if v is not None else "N/A"


def render(d, out_dir):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    tr = d["trade"]; r = d["roc"]; tk = d["ticker"]; dt = d["date"]
    et, xt = tr["entry_t_sec"], tr["exit_t_sec"]; ep, xp = tr["entry_price"], tr["exit_price"]
    pnl, reason, hold = tr["pnl_pct"], tr["exit_reason"], tr["hold_sec"]
    win = pnl > 0; lo, hi = d["win_lo"], d["win_hi"]
    roc_lab = _roc_label(r)
    roc_v = r.get("scanner_roc_5m_at_fire"); gap = r.get("gap_pct_at_hit", float("nan"))
    roc_title = f"{roc_v:.3f}" if roc_v is not None else "N/A"

    fig = make_subplots(rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.03,
                        row_heights=[0.35, 0.20, 0.25, 0.20],
                        subplot_titles=["Price (30s OHLC) + entry/exit + EPG PASS + T_gate check",
                                        "Sell intensity I(t) = λ_sell/(λ_buy+λ_sell)",
                                        "λ_V / peak ratio (re-entry disabled)", "EPG gate state"])
    for s0, s1, s in d["state_intervals"]:
        if s == "PASS":
            for rw in (1, 2, 3):
                fig.add_shape(type="rect", x0=s0, x1=s1, y0=0, y1=1, xref="x", yref="y domain",
                              row=rw, col=1, fillcolor="rgba(0,200,100,0.12)", line_width=0, layer="below")
    if d["bt"]:
        fig.add_trace(go.Candlestick(x=d["bt"], open=d["bo"], high=d["bh"], low=d["bl"], close=d["bc"],
                      increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
                      showlegend=False), row=1, col=1)
    fig.add_trace(go.Scatter(x=[et], y=[ep], mode="markers+text",
                  marker=dict(symbol="triangle-up", size=14, color="green"),
                  text=[f"Entry @ {ep:.4f} | roc_5m: {roc_lab}"], textposition="top center",
                  textfont=dict(size=10, color="green"), name="Entry"), row=1, col=1)
    xc = "orange" if reason == "time_gate" else (("green" if win else "red") if reason == "epg_window_close" else "gray")
    xlab = {"time_gate": f"Time Gate @ {xp:.4f}", "epg_window_close": f"EPG Close @ {xp:.4f}"}.get(reason, "Session End")
    fig.add_trace(go.Scatter(x=[xt], y=[xp], mode="markers+text",
                  marker=dict(symbol="triangle-down", size=14, color=xc),
                  text=[xlab], textposition="bottom center", textfont=dict(size=10, color=xc),
                  name=f"Exit ({reason})"), row=1, col=1)
    fig.add_vline(x=et + T_GATE, line=dict(color="orange", dash="dash", width=1.5),
                  annotation_text="T_gate +500s", annotation_position="top", row=1, col=1)
    if d["scanner_t"] is not None and lo <= d["scanner_t"] <= hi:
        fig.add_vline(x=d["scanner_t"], line=dict(color="blue", dash="dash", width=1.2),
                      annotation_text="Scanner Hit", annotation_position="top left", row=1, col=1)
    fig.add_trace(go.Scatter(x=d["tw"], y=d["isell"], mode="lines",
                  line=dict(color="#8888cc", width=1.1), name="I(t) sell"), row=2, col=1)
    fig.add_hline(y=THETA, line=dict(color="gray", dash="dot", width=1.0),
                  annotation_text=f"theta={THETA}", annotation_position="bottom right", row=2, col=1)
    fig.add_trace(go.Scatter(x=d["tw"], y=d["ratio"], mode="lines",
                  line=dict(color="#999999", width=1.5), name="λ_V / peak"), row=3, col=1)
    fig.add_hline(y=P_CLOSE, line=dict(color="red", dash="dash", width=1.3),
                  annotation_text=f"p_close={P_CLOSE}", annotation_position="bottom right", row=3, col=1)
    fig.add_hline(y=1.0, line=dict(color="gray", dash="dash", width=1.0),
                  annotation_text="peak", annotation_position="top right", row=3, col=1)
    for s0, s1, s in d["state_intervals"]:
        fig.add_shape(type="rect", x0=s0, x1=s1, y0=0, y1=1, xref="x", yref="y domain", row=4, col=1,
                      fillcolor=_STATE.get(s, "rgba(200,200,200,0.85)"), line_width=0, layer="below")
    for s, c in _STATE.items():
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                      marker=dict(size=10, color=c, symbol="square"), name=s), row=4, col=1)
    fig.update_layout(
        title=dict(text=f"{tk} {dt} | ROC disabled | {reason} | PnL: {pnl:+.2f}% | roc_5m: {roc_title} | gap: {gap:.1f}%",
                   font=dict(size=14)),
        height=1100, width=1400, hovermode="x unified", xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1,
                    bgcolor="rgba(255,255,255,0.6)"))
    fig.update_xaxes(range=[lo, hi]); fig.update_xaxes(title_text="Seconds from first trade (t_sec)", row=4, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="I(t)", range=[0, 1], row=2, col=1)
    fig.update_yaxes(title_text="λ_V/peak", range=[0.0, 1.1], row=3, col=1)
    fig.update_yaxes(title_text="state", range=[0, 1], showticklabels=False, row=4, col=1)
    fig.add_annotation(text="Panel 3: λ_V/peak ratio (re-entry disabled)", xref="paper", yref="paper",
                       x=0.0, y=-0.05, showarrow=False, font=dict(size=10, color="#666"))
    fig.write_html(str(out_dir / f"{tk}_{dt}.html"), include_plotlyjs="cdn", config={"responsive": True})


def build_index(rows, out_dir):
    def cell(v, nd=2):
        if v is None:
            return "—"
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    body = "\n".join(
        f'<tr><td>{r["ticker"]}</td><td>{r["date"]}</td><td>{r["stratum"]}</td>'
        f'<td>{r["exit_reason"]}</td><td>{r["hold_sec"]:.0f}</td><td>{r["pnl_pct"]:+.2f}</td>'
        f'<td data-v="{(r["roc"] if r["roc"] is not None else -999)}">{cell(r["roc"],3)}</td>'
        f'<td>{str(r["first"])}</td>'
        f'<td><a href="{r["ticker"]}_{r["date"]}.html" target="_blank">chart</a></td></tr>'
        for r in rows)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>R3 ROC-disabled — per-event charts</title>
<style>
body{{font-family:monospace;font-size:13px;padding:16px}}
table{{border-collapse:collapse;width:100%;max-width:1000px}}
th,td{{border:1px solid #ccc;padding:4px 10px;text-align:right}}
th{{background:#37474f;color:#fff;cursor:pointer}} td:first-child,td:nth-child(2),td:nth-child(3),td:nth-child(4){{text-align:left}}
tr:hover{{background:#f5f5f5}} a{{color:#1565c0;text-decoration:none}}
</style><script>
let _c=-1,_a=true;function st(n){{var t=document.getElementById('t');var rs=Array.from(t.tBodies[0].rows);
_a=(_c===n)?!_a:true;_c=n;rs.sort(function(a,b){{
var av=a.cells[n].getAttribute('data-v'),bv=b.cells[n].getAttribute('data-v');
var x=av!==null?av:a.cells[n].innerText,y=bv!==null?bv:b.cells[n].innerText;
var xn=parseFloat(x),yn=parseFloat(y);if(!isNaN(xn)&&!isNaN(yn))return _a?xn-yn:yn-xn;
return _a?String(x).localeCompare(y):String(y).localeCompare(x);}});
rs.forEach(function(r){{t.tBodies[0].appendChild(r);}});}}
</script></head><body>
<h2>Phase R3 — ROC disabled arm — per-event charts ({len(rows)} traded events)</h2>
<p>val_r3_stratified, p=0.65, T_gate=500. roc_5m nulls (first-appearance) sort last.</p>
<table id="t"><thead><tr>
<th onclick="st(0)">Ticker</th><th onclick="st(1)">Date</th><th onclick="st(2)">Stratum</th>
<th onclick="st(3)">Exit</th><th onclick="st(4)">Hold(s)</th><th onclick="st(5)">PnL%</th>
<th onclick="st(6)">roc_5m</th><th onclick="st(7)">1st appear</th><th>Chart</th>
</tr></thead><tbody>
{body}
</tbody></table></body></html>"""
    (out_dir / "index.html").write_text(html, encoding="utf-8")


def main():
    trades = json.load(open(ARM_DIR / "per_trade.json"))
    roc = {(r["ticker"], r["date"]): r for r in json.load(open(ROC_VALUES))}
    ev = {(e["ticker"], e["date"]): e for e in json.load(open(SAMPLE))["events"]}
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hm = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        qcfg = json.load(f)
    pep = {}
    pa = REPO / "results" / "phase_a" / "production_fit_results.json"
    if pa.exists():
        for rr in json.load(open(pa)):
            if rr.get("status") == "success" and "final_params" in rr:
                pep[(rr["ticker"], rr["date"])] = rr["final_params"]
    rho = hm.get("rho", 0.99)
    tasks = []
    for t in trades:
        k = (t["ticker"], t["date"]); meta = ev.get(k)
        if meta is None:
            print(f"  WARN {k} not in sample"); continue
        tasks.append({"ticker": t["ticker"], "date": t["date"], "mom_pct": meta["mom_pct"],
                      "trade": t, "roc": roc.get(k, {}), "fp": pep.get(k, hm), "rho": rho,
                      "q_bar_cfg": qcfg, "scanner_hit_ts_ns": meta.get("scanner_hit_ts_ns")})
    print(f"disabled arm: {len(tasks)} traded events, collecting (6 workers)...")
    res = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(collect, t): t for t in tasks}
        done = 0
        for fu in as_completed(futs):
            r = fu.result(); done += 1; res.append(r)
            if r["status"] != "ok":
                print(f"  [{done}/{len(tasks)}] {r['status']} {r['ticker']} {r['date']}: {r.get('reason') or r.get('error')}")
            elif done % 15 == 0:
                print(f"  [{done}/{len(tasks)}] collected")
    ok = [r for r in res if r["status"] == "ok"]
    print(f"Collected {len(ok)}/{len(tasks)}; rendering...")
    rows = []
    for r in ok:
        try:
            render(r, CHART_DIR)
        except Exception as e:
            print(f"  render FAIL {r['ticker']} {r['date']}: {e}"); continue
        tr = r["trade"]; rc = r["roc"]
        rows.append({"ticker": r["ticker"], "date": r["date"], "stratum": rc.get("stratum", "?"),
                     "exit_reason": tr["exit_reason"], "hold_sec": tr["hold_sec"], "pnl_pct": tr["pnl_pct"],
                     "roc": rc.get("scanner_roc_5m_at_fire"), "first": bool(rc.get("is_first_appearance"))})
    rows.sort(key=lambda x: (x["stratum"], x["ticker"], x["date"]))
    build_index(rows, CHART_DIR)
    print(f"{len(rows)} charts + index written to {CHART_DIR}")


if __name__ == "__main__":
    main()
