#!/usr/bin/env python3
"""FIX-T4E-T6B / R1-FIXED T8 — Per-event 4-panel charts for the p=0.70 arm.

Panel layout (per phase prompt T8, adapting Agent_Prompt_Standard.md §7):
  1. Price        — 30s OHLC + entry (green ▲) + exit (green/red ▼, labeled reason)
                    + EPG PASS-window shading (green, opacity 0.12)
  2. Sell I(t)    — I(t) = λ_sell/(λ_buy+λ_sell), theta reference line. No EXIT_D markers.
  3. λ_V/peak     — ratio (gray), p_close=0.70 dashed red, peak=1.0 line.
                    SUBSTITUTION: re-entry is disabled, so buy intensity is omitted;
                    this panel plots the gate participation ratio instead. Primary
                    diagnostic — ratio should cross below 0.70 at epg_window_close exits.
  4. EPG state    — PASS / FAIL / WARMUP / INACTIVE as colored bands.

Traces are reconstructed with the FIXED runner's own helpers
(_hawkes_replay_with_refit, _build_halt_intervals) and a config-exact gate
(gate_mode=peak, tau_peak=600, C=1.5, EPG_TAU=300, warmup=300, p_open=p_close=0.70),
including the T6b halt dt-substitution on the gate clock. Entry/exit markers are
taken from the booked per_trade.json (authoritative fixed-runner result).

Collection runs in parallel workers; each worker windows its traces to
[entry-600s, exit+120s] before returning (compact payload). Rendering is sequential.

Run:  python -m backtest.r1_fixed_charts
"""
from __future__ import annotations

import json
import math
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

import numpy as np

from backtest.runner_rapid import (
    _hawkes_replay_with_refit,
    _build_halt_intervals,
    EPG_K, EPG_TAU, EPG_WARMUP, HALT_GAP_THRESHOLD,
)
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from data.loaders.trades import (
    load_trades, _session_ns_bounds, compute_lambda_ref_per_event,
)
from data.loaders.quotes import load_quotes
from data.loaders.prev_close import get_prev_close
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState

BACKTEST = Path(__file__).resolve().parent
REPO = BACKTEST.parent
ARM_DIR = BACKTEST / "results" / "phase_r1_fixed" / "sym_p70"
CHART_DIR = BACKTEST / "results" / "phase_r1_fixed" / "event_charts"
EVENT_FILE = Path(r"D:\Trading Research\data\val_mdr150_diagnostic.json")

P_OPEN = 0.70
P_CLOSE = 0.70
GATE_MODE = "peak"
TAU_PEAK = 600.0
GATE_C = 1.5
THETA = 0.65
WIN_PRE = 600.0
WIN_POST = 120.0

CHART_DIR.mkdir(parents=True, exist_ok=True)

_STATE_SOLID = {
    "INACTIVE": "rgba(190,190,190,0.85)",
    "WARMUP":   "rgba(255,165,0,0.85)",
    "PASS":     "rgba(60,180,60,0.85)",
    "FAIL":     "rgba(210,60,60,0.85)",
}


def _build_30s_bars(timestamps_ns, prices, session_start_ns):
    BAR_NS = 30 * NS_PER_SECOND
    if len(timestamps_ns) == 0:
        e = np.array([], dtype=np.float64)
        return np.array([], dtype=np.int64), e, e, e, e
    bar_idx = ((timestamps_ns.astype(np.int64) - session_start_ns) // BAR_NS).astype(np.int64)
    starts, opens, highs, lows, closes = [], [], [], [], []
    for bi in np.unique(bar_idx):
        bp = prices[bar_idx == bi]
        starts.append(int(session_start_ns + bi * BAR_NS))
        opens.append(float(bp[0])); highs.append(float(np.max(bp)))
        lows.append(float(np.min(bp))); closes.append(float(bp[-1]))
    return (np.array(starts, dtype=np.int64), np.array(opens),
            np.array(highs), np.array(lows), np.array(closes))


def _state_intervals(t_sec_list, states):
    if not t_sec_list:
        return []
    out = []
    cur = states[0]; start = t_sec_list[0]
    for i in range(1, len(t_sec_list)):
        if states[i] != cur:
            out.append((start, t_sec_list[i], cur))
            cur = states[i]; start = t_sec_list[i]
    out.append((start, t_sec_list[-1], cur))
    return out


def collect_windowed(task):
    """Worker: reconstruct traces, window them, return a compact render dict."""
    ticker = task["ticker"]; date = task["date"]
    trade = task["trade"]
    fp = task["fp"]; rho = task["rho"]
    q_bar_cfg = task["q_bar_cfg"]; scanner_hit_ts_ns = task["scanner_hit_ts_ns"]
    try:
        td = load_trades(ticker, date, task["mom_pct"])
        if td is None or td.n_trades < 30:
            return {"status": "skip", "ticker": ticker, "date": date, "reason": "trades"}
        qd = load_quotes(ticker, date, task["mom_pct"])
        if qd is None or qd.n_quotes < 10:
            return {"status": "skip", "ticker": ticker, "date": date, "reason": "quotes"}
        prev_close = get_prev_close(ticker, date)
        if prev_close is None or prev_close <= 0:
            return {"status": "skip", "ticker": ticker, "date": date, "reason": "prev_close"}

        N = td.n_trades
        start_ns, _ = _session_ns_bounds(date)
        scanner_t_sec = None
        if scanner_hit_ts_ns is not None:
            scanner_t_sec = (int(scanner_hit_ts_ns) - int(td.timestamps[0])) / NS_PER_SECOND

        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps,
            quote_bid_prices=qd.bid_prices, quote_ask_prices=qd.ask_prices,
            quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides = ofi.sides

        halt_intervals = _build_halt_intervals(td)
        lam_buy = np.zeros(N); lam_sell = np.zeros(N)
        E_out = np.zeros(N); Edot = np.zeros(N); n_base = np.zeros(N)
        dv_arr = td.prices.astype(np.float64) * td.sizes.astype(np.float64)
        global_lref = fp["mu_buy"] + fp["mu_sell"]
        per_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = global_lref if (math.isnan(per_lref) or per_lref <= 0) else per_lref

        cold = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=rho, lambda_ref=lambda_ref,
            init_params=fp, rho_E=rho, lam_buy_out=lam_buy, lam_sell_out=lam_sell,
            E_out=E_out, Edot_out=Edot, n_base_out=n_base, dv_arr=dv_arr,
            halt_intervals=halt_intervals or None,
        )
        lambda_hat = lam_buy + lam_sell

        lref_epg = fp["mu_buy"] + fp["mu_sell"]
        anchor = EventAnchor(lambda_ref=lref_epg, k_multiplier=EPG_K)
        if cold is not None and (cold.mu_buy + cold.mu_sell) > 0:
            anchor.set_lambda_ref(cold.mu_buy + cold.mu_sell)
        gate = ParticipationGate(
            half_life_seconds=EPG_TAU, peak_threshold_p=P_OPEN,
            warmup_seconds=EPG_WARMUP, gate_mode=GATE_MODE,
            tau_peak=TAU_PEAK, C=GATE_C, p_open=P_OPEN, p_close=P_CLOSE,
        )

        states = []
        ratio = np.full(N, np.nan)
        fired = False
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None and not fired:
                gate.activate(t_ev); fired = True
            gate_t = td.t_sec[i]
            if i > 0 and halt_intervals and td.t_sec[i] - td.t_sec[i - 1] > HALT_GAP_THRESHOLD:
                for hs, he in halt_intervals:
                    if td.t_sec[i - 1] < he and td.t_sec[i] > hs:
                        gate_t = td.t_sec[i - 1] + 1e-6
                        break
            st = gate.update(float(td.prices[i]) * float(td.sizes[i]), gate_t)
            states.append(st.name)
            if gate._lambda_v_peak > 0:
                ratio[i] = gate._lambda_v / gate._lambda_v_peak
        if not fired:
            return {"status": "skip", "ticker": ticker, "date": date, "reason": "no_t_event"}

        I_sell = np.where((lam_buy + lam_sell) > 0, lam_sell / (lam_buy + lam_sell), np.nan)

        # Window
        win_lo = max(float(td.t_sec[0]), trade["entry_t_sec"] - WIN_PRE)
        win_hi = min(float(td.t_sec[-1]), trade["exit_t_sec"] + WIN_POST)
        t = td.t_sec
        m = (t >= win_lo) & (t <= win_hi)
        tw = t[m]
        nW = len(tw)
        step = max(1, nW // 20000) if nW > 40000 else 1

        # state intervals clipped to window
        si = [(max(s0, win_lo), min(s1, win_hi), st)
              for (s0, s1, st) in _state_intervals(t.tolist(), states)
              if not (s1 < win_lo or s0 > win_hi)]

        b_starts, b_o, b_h, b_l, b_c = _build_30s_bars(td.timestamps, td.prices, start_ns)
        b_t = (b_starts.astype(np.float64) - int(td.timestamps[0])) / NS_PER_SECOND
        bm = (b_t >= win_lo) & (b_t <= win_hi)

        return {
            "status": "ok", "ticker": ticker, "date": date,
            "trade": trade, "scanner_t_sec": scanner_t_sec,
            "win_lo": win_lo, "win_hi": win_hi,
            "tw": tw[::step].tolist(),
            "ratio": ratio[m][::step].tolist(),
            "isell": I_sell[m][::step].tolist(),
            "state_intervals": si,
            "b_t": (b_t[bm] + 15.0).tolist(),
            "b_o": b_o[bm].tolist(), "b_h": b_h[bm].tolist(),
            "b_l": b_l[bm].tolist(), "b_c": b_c[bm].tolist(),
        }
    except Exception as e:
        import traceback
        return {"status": "error", "ticker": ticker, "date": date,
                "error": str(e), "tb": traceback.format_exc()[-800:]}


def render(d, out_dir):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots

    tr = d["trade"]
    ticker, date = d["ticker"], d["date"]
    entry_t, exit_t = tr["entry_t_sec"], tr["exit_t_sec"]
    entry_px, exit_px = tr["entry_price"], tr["exit_price"]
    pnl, reason, hold = tr["pnl_pct"], tr["exit_reason"], tr["hold_sec"]
    win = pnl > 0
    win_lo, win_hi = d["win_lo"], d["win_hi"]

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.035,
        row_heights=[0.38, 0.22, 0.22, 0.18],
        subplot_titles=[
            "Price (30s OHLC)  +  entry/exit  +  EPG PASS shading",
            "Sell intensity  I(t) = λ_sell / (λ_buy + λ_sell)",
            "λ_V / peak ratio  (re-entry disabled; buy intensity omitted)",
            "EPG gate state",
        ],
    )

    for s0, s1, st in d["state_intervals"]:
        if st != "PASS":
            continue
        for row in (1, 2, 3):
            fig.add_shape(type="rect", x0=s0, x1=s1, y0=0, y1=1,
                          xref="x", yref="y domain", row=row, col=1,
                          fillcolor="rgba(0,200,100,0.12)", line_width=0, layer="below")

    if d["b_t"]:
        fig.add_trace(go.Candlestick(
            x=d["b_t"], open=d["b_o"], high=d["b_h"], low=d["b_l"], close=d["b_c"],
            increasing_line_color="#2ca02c", decreasing_line_color="#d62728",
            showlegend=False, name="30s OHLC"), row=1, col=1)

    fig.add_trace(go.Scatter(x=[entry_t], y=[entry_px], mode="markers",
                  marker=dict(symbol="triangle-up", size=14, color="green"),
                  name="Entry"), row=1, col=1)
    ec = "green" if win else "red"
    fig.add_trace(go.Scatter(x=[exit_t], y=[exit_px], mode="markers+text",
                  marker=dict(symbol="triangle-down", size=14, color=ec),
                  text=[reason], textposition="bottom center",
                  textfont=dict(size=10, color=ec),
                  name=f"Exit ({reason})"), row=1, col=1)

    if d["scanner_t_sec"] is not None and win_lo <= d["scanner_t_sec"] <= win_hi:
        fig.add_vline(x=d["scanner_t_sec"], line_color="royalblue", line_width=1.2,
                      line_dash="dot", annotation_text="scanner",
                      annotation_position="top left")

    fig.add_trace(go.Scatter(x=d["tw"], y=d["isell"], mode="lines",
                  line=dict(color="#8888cc", width=1.1), name="I(t) sell"), row=2, col=1)
    fig.add_hline(y=THETA, line_dash="dot", line_color="gray", line_width=1.0,
                  annotation_text=f"theta={THETA} (EXIT_D off)",
                  annotation_position="bottom right", row=2, col=1)

    fig.add_trace(go.Scatter(x=d["tw"], y=d["ratio"], mode="lines",
                  line=dict(color="#999999", width=1.2), name="λ_V / peak"), row=3, col=1)
    fig.add_hline(y=P_CLOSE, line_dash="dash", line_color="red", line_width=1.3,
                  annotation_text=f"p_close={P_CLOSE}",
                  annotation_position="bottom right", row=3, col=1)
    fig.add_hline(y=1.0, line_dash="dash", line_color="#888", line_width=1.0,
                  annotation_text="peak=1.0", annotation_position="top right", row=3, col=1)

    for s0, s1, st in d["state_intervals"]:
        fig.add_shape(type="rect", x0=s0, x1=s1, y0=0, y1=1,
                      xref="x", yref="y domain", row=4, col=1,
                      fillcolor=_STATE_SOLID.get(st, "rgba(200,200,200,0.85)"),
                      line_width=0, layer="below")
    for st, color in _STATE_SOLID.items():
        fig.add_trace(go.Scatter(x=[None], y=[None], mode="markers",
                      marker=dict(size=10, color=color, symbol="square"),
                      name=st), row=4, col=1)

    pnl_s = f"+{pnl:.2f}%" if pnl >= 0 else f"{pnl:.2f}%"
    fig.update_layout(
        title=dict(text=(f"{ticker}  {date}  |  {reason}  |  PnL {pnl_s}  |  "
                         f"hold {hold:.0f}s  |  p=0.70"), font=dict(size=13)),
        height=950, hovermode="x unified", xaxis_rangeslider_visible=False,
        legend=dict(orientation="h", yanchor="bottom", y=1.03, xanchor="right", x=1),
    )
    fig.update_xaxes(range=[win_lo, win_hi])
    fig.update_xaxes(title_text="Seconds from first trade (t_sec)", row=4, col=1)
    fig.update_yaxes(title_text="Price", row=1, col=1)
    fig.update_yaxes(title_text="I(t) sell", range=[0, 1], row=2, col=1)
    fig.update_yaxes(title_text="λ_V/peak", row=3, col=1)
    fig.update_yaxes(title_text="state", range=[0, 1], showticklabels=False, row=4, col=1)
    fig.add_annotation(
        text=("Panel 3: λ_V/peak ratio (re-entry disabled; buy intensity omitted). "
              "PASS→FAIL exit fires when ratio &lt; p_close=0.70."),
        xref="paper", yref="paper", x=0.0, y=-0.06, showarrow=False,
        font=dict(size=10, color="#666"), align="left")

    fig.write_html(str(out_dir / f"{ticker}_{date}.html"),
                   include_plotlyjs="cdn", config={"responsive": True})


def build_index(rows, out_dir):
    def _fmt(v, nd=2):
        if v is None:
            return "—"
        return f"{v:.{nd}f}" if isinstance(v, float) else str(v)
    body = "\n".join(
        f'<tr><td>{r["ticker"]}</td><td>{r["date"]}</td><td>{r["exit_reason"]}</td>'
        f'<td>{_fmt(r["hold_sec"], 0)}</td><td>{_fmt(r["pnl_pct"])}</td>'
        f'<td>{_fmt(r["n_passtofail"], 0)}</td>'
        f'<td><a href="{r["ticker"]}_{r["date"]}.html" target="_blank">chart</a></td></tr>'
        for r in rows)
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<title>R1-FIXED p=0.70 — Per-event charts</title>
<style>
body{{font-family:monospace;font-size:13px;padding:16px}}
table{{border-collapse:collapse;width:100%;max-width:900px}}
th,td{{border:1px solid #ccc;padding:4px 10px;text-align:right}}
th{{background:#eee;cursor:pointer;user-select:none}}
td:first-child,td:nth-child(2),td:nth-child(3){{text-align:left}}
tr:hover{{background:#f5f5f5}} a{{color:#1a73e8;text-decoration:none}}
</style>
<script>
let _c=-1,_a=true;
function st(n){{var t=document.getElementById('t');var rs=Array.from(t.tBodies[0].rows);
_a=(_c===n)?!_a:true;_c=n;rs.sort(function(a,b){{
var x=a.cells[n].innerText.replace('—',''),y=b.cells[n].innerText.replace('—','');
var xn=parseFloat(x),yn=parseFloat(y);
if(!isNaN(xn)&&!isNaN(yn))return _a?xn-yn:yn-xn;
return _a?x.localeCompare(y):y.localeCompare(x);}});
rs.forEach(function(r){{t.tBodies[0].appendChild(r);}});}}
</script></head><body>
<h2>R1-FIXED (T4e+T6b) — p_open=p_close=0.70 — per-event charts</h2>
<p>{len(rows)} traded events. Click headers to sort.
Panel 3 = λ_V/peak ratio (re-entry disabled; buy intensity omitted).</p>
<table id="t"><thead><tr>
<th onclick="st(0)">Ticker</th><th onclick="st(1)">Date</th>
<th onclick="st(2)">Exit Reason</th><th onclick="st(3)">Hold (s)</th>
<th onclick="st(4)">PnL %</th><th onclick="st(5)">PASS→FAIL</th><th>Chart</th>
</tr></thead><tbody>
{body}
</tbody></table></body></html>"""
    with open(out_dir / "index.html", "w", encoding="utf-8") as f:
        f.write(html)


def main():
    with open(ARM_DIR / "per_trade.json") as f:
        trades = json.load(f)
    with open(ARM_DIR / "per_event_summary.json") as f:
        per_event = json.load(f)
    ptf = {(e["ticker"], e["date"], e["event_idx"]): e.get("n_passtofail_transitions", 0)
           for e in per_event if e.get("status") == "event"}
    with open(EVENT_FILE) as f:
        ev_meta = {(e["ticker"], e["date"]): e for e in json.load(f)["events"]}
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_median = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)
    phase_a_path = REPO / "results" / "phase_a" / "production_fit_results.json"
    pep = {}
    if phase_a_path.exists():
        with open(phase_a_path) as f:
            for r in json.load(f):
                if r.get("status") == "success" and "final_params" in r:
                    pep[(r["ticker"], r["date"])] = r["final_params"]

    rho = hawkes_median.get("rho", 0.99)
    tasks = []
    for tr in trades:
        meta = ev_meta.get((tr["ticker"], tr["date"]))
        if meta is None:
            print(f"  WARN {tr['ticker']} {tr['date']}: not in event file"); continue
        tasks.append({
            "ticker": tr["ticker"], "date": tr["date"], "mom_pct": meta["mom_pct"],
            "trade": tr, "fp": pep.get((tr["ticker"], tr["date"]), hawkes_median),
            "rho": rho, "q_bar_cfg": q_bar_cfg,
            "scanner_hit_ts_ns": meta.get("scanner_hit_ts_ns"),
        })

    print(f"p=0.70 arm: {len(tasks)} traded events, collecting (6 workers)...")
    results = []
    with ProcessPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(collect_windowed, t): t for t in tasks}
        done = 0
        for fut in as_completed(futs):
            r = fut.result(); done += 1
            results.append(r)
            tag = r["status"]
            if tag == "error":
                print(f"  [{done}/{len(tasks)}] ERROR {r['ticker']} {r['date']}: {r['error']}")
            elif tag == "skip":
                print(f"  [{done}/{len(tasks)}] skip {r['ticker']} {r['date']}: {r['reason']}")
            elif done % 10 == 0:
                print(f"  [{done}/{len(tasks)}] collected")

    ok = [r for r in results if r["status"] == "ok"]
    print(f"Collected {len(ok)}/{len(tasks)}; rendering...")
    index_rows = []
    for r in ok:
        try:
            render(r, CHART_DIR)
        except Exception as e:
            print(f"  render FAIL {r['ticker']} {r['date']}: {e}")
            continue
        tr = r["trade"]
        index_rows.append({
            "ticker": r["ticker"], "date": r["date"],
            "exit_reason": tr["exit_reason"], "hold_sec": tr["hold_sec"],
            "pnl_pct": tr["pnl_pct"],
            "n_passtofail": ptf.get((r["ticker"], r["date"], tr["event_idx"]), 0),
        })
    index_rows.sort(key=lambda x: (x["exit_reason"], -x["pnl_pct"]))
    build_index(index_rows, CHART_DIR)
    print(f"\n{len(index_rows)} charts + index written to {CHART_DIR}")


if __name__ == "__main__":
    main()
