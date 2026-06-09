"""
Phase CPD-DIAG — CUSUM diagnostic charts for k12_h8 (15-event subsample)
========================================================================
Faithful reconstruction of the EXACT signal the k=12, h=8 CUSUM gate ran on, so the
−30% tail behaviour can be inspected visually. Diagnostic only — no backtest changes.

Locked decisions carried in (Cooper):
  - WJI_background ≡ 1.0  →  WJI_log(t) = log(WJI(t))  (Gate-1 Option A; Panel 2 background
    line is therefore flat at 1.0). The dynamic-background framing in the CPD-DIAG plan is
    NOT used — it would show a different signal than the gate actually used.
  - T2a (warmup-centering) is reported DESCRIPTIVELY, not as a hard stop: the warmup window
    is the ignition surge (mean WJI_log ≈ +1.25, established in CPD-0), so "centered near 0"
    is not expected and is not a failure here.
  - Timestamp axis = halt-adjusted active seconds since T_event (same as CPD-1).

15 events drawn with numpy.random.default_rng(7), no replacement, from the 100-event val
sample. k=12, h=8, sigma_log_fallback=0.209.

PELT (T2b): run offline on the WJI_log trace, cost "rbf", penalty pen=log(n)*sigma_log**2.
The raw tick trace is far too long for rbf PELT, so it is resampled to a uniform active-time
grid (~3000 bins, mean log per bin) first — documented deviation for tractability. If PELT
returns > 10 changepoints the penalty is scaled up until ≤ 10 (the plan's cap).

Outputs (results/phase_cpd_diag/):
  selected_events.json, pelt_changepoints.json, signal_stats.json,
  charts/{TICKER}_{DATE}.html (15), charts/index.html

Run:
  "D:/Trading Research/.venv/Scripts/python.exe" -m tools.phase_cpd.diag_charts
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, _session_ns_bounds
from data.loaders.quotes import load_quotes
from core.ofi.trade_ofi import compute_trade_ofi
from core.epg.gate import ParticipationGate, GateState
from tools.sweep_runner_opt2 import precompute_sf_trajectory, sf_is_qualified_at
from tools.phase_cpd.cpd0_t1_traces import _build_active_axis, _compute_wji_active

import ruptures as rpt

K, H = 12.0, 8.0
SIGMA_FALLBACK = 0.209
EPG_WARMUP = 300.0
PELT_TARGET_BINS = 3000
PELT_MAX_CP = 10
PLOT_MAX_POINTS = 4000       # downsample per-tick panels to keep HTML small

OUT_DIR = REPO_ROOT / "results" / "phase_cpd_diag"
CHART_DIR = OUT_DIR / "charts"
SAMPLE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / "quality_sample_val.json"
CACHE_PATH = REPO_ROOT / "results" / "phase_wji_poc" / ".cache_val_results.json"
QBAR_PATH = REPO_ROOT / "config" / "q_bar_tiers.json"


# ══════════════════════════════════════════════════════════════════════
#  Reconstruction + replay (with per-tick S_up / state capture)
# ══════════════════════════════════════════════════════════════════════

def _reconstruct(args: dict) -> dict:
    """Reconstruct the k12_h8 signal + trades for one event; build & write its chart."""
    ticker, date, mom_pct = args["ticker"], args["date"], args["mom_pct"]
    t_event_raw, mu_buy = args["t_event"], args["mu_buy"]
    q_bar_cfg, val_index = args["q_bar_cfg"], args["val_index"]
    year = date[:4]
    base = {"ticker": ticker, "date": date, "year": year, "val_index": val_index}

    try:
        td = load_trades(ticker, date, mom_pct)
        qd = load_quotes(ticker, date, mom_pct)
        if td.n_trades < 30 or qd is None or qd.n_quotes < 10:
            return {**base, "status": "error", "error": "insufficient trade/quote data"}

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
        sides_full = ofi.sides

        mask, active_seconds, n_halts, halt_seconds = _build_active_axis(td)
        prices_a = td.prices[mask].astype(np.float64)
        sizes_a = td.sizes[mask]
        sides_a = sides_full[mask]
        ts_ns_a = td.timestamps[mask]
        t_sec_a = td.t_sec[mask]
        pos = int(np.searchsorted(t_sec_a, t_event_raw, side="right")) - 1
        t_event_active = float(active_seconds[max(pos, 0)])

        wji, _ = _compute_wji_active(prices_a, sizes_a, sides_a, active_seconds,
                                     t_event_active, mu_buy)
        wji_log = np.log(np.where(wji > 0, wji, 1e-9))  # bg ≡ 1.0
        tse = active_seconds - t_event_active           # active sec since T_event

        # ── Replay k12_h8, capturing per-tick S_up + state ──
        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)
        use_sf = sf is not None and sf.n_bars > 0

        gate = ParticipationGate(
            half_life_seconds=300.0, peak_threshold_p=0.65, warmup_seconds=EPG_WARMUP,
            gate_mode="cusum", k=K, h=H, sigma_log_fallback=SIGMA_FALLBACK,
        )
        gate.activate(t_event_active)

        N = len(wji)
        s_up = np.zeros(N); state_code = np.zeros(N, dtype=np.int8)  # 0 FAIL,1 WARMUP,2 PASS,-1 INACTIVE
        max_from = np.maximum.accumulate(prices_a[::-1])[::-1]
        prev = GateState.INACTIVE
        in_pos = False
        entry_t = entry_price = entry_idx = None
        entries, exits = [], []  # (active_sec, price)
        trade_pnls = []
        for i in range(N):
            st = gate.update(wji=float(wji[i]), timestamp=float(active_seconds[i]), wji_background=1.0)
            s_up[i] = gate.s_up
            state_code[i] = {GateState.WARMUP: 1, GateState.PASS: 2, GateState.FAIL: 0}.get(st, -1)
            if not in_pos:
                if st == GateState.PASS and prev in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL):
                    if (not use_sf) or sf_is_qualified_at(sf, int(ts_ns_a[i])):
                        in_pos = True
                        entry_idx = min(i + 1, N - 1)
                        entry_t = float(tse[i]); entry_price = float(prices_a[entry_idx])
                        entries.append((entry_t, entry_price))
            else:
                if prev == GateState.PASS and st != GateState.PASS:
                    xidx = min(i + 1, N - 1)
                    xprice = float(prices_a[xidx])
                    exits.append((float(tse[i]), xprice))
                    trade_pnls.append((xprice - entry_price) / entry_price * 100.0)
                    in_pos = False; entry_t = entry_price = entry_idx = None
            prev = st
        if in_pos:
            xprice = float(prices_a[N - 1])
            exits.append((float(tse[N - 1]), xprice))
            trade_pnls.append((xprice - entry_price) / entry_price * 100.0)

        sigma_log = float(gate.sigma_log) if gate.sigma_log is not None else SIGMA_FALLBACK

        # ── WARMUP-window WJI_log stats (T2a, descriptive) ──
        wmask = (tse >= 0.0) & (tse < EPG_WARMUP)
        wl = wji_log[wmask]
        warmup_mean = float(wl.mean()) if len(wl) else float("nan")
        warmup_std = float(wl.std(ddof=1)) if len(wl) > 1 else float("nan")

        # ── PELT on WJI_log (resampled full active trace) ──
        cps_sec, n_cp = _run_pelt(active_seconds, wji_log, t_event_active, sigma_log)

        # ── Trade / tail stats ──
        total_pnl = float(sum(trade_pnls))
        worst = float(min(trade_pnls)) if trade_pnls else 0.0
        tail_label = ("TAIL EVENT" if worst <= -20.0 else
                      "MODERATE LOSS" if worst <= -10.0 else "OK")
        n_pass_windows = int(np.sum((state_code[1:] == 2) & (state_code[:-1] != 2)))

        # ── Build + write chart ──
        _build_chart(
            ticker, date, tse, prices_a, wji, wji_log, s_up, state_code,
            entries, exits, trade_pnls, sigma_log, cps_sec, n_halts,
            total_pnl, tail_label, CHART_DIR / f"{ticker}_{date}.html",
        )

        return {
            **base, "status": "ok",
            "n_trades": len(trade_pnls), "total_pnl_pct": total_pnl,
            "worst_trade_pnl_pct": worst, "tail_label": tail_label,
            "n_pelt_changepoints": n_cp, "n_cusum_pass_windows": n_pass_windows,
            "sigma_log": sigma_log, "warmup_wji_log_mean": warmup_mean,
            "warmup_wji_log_std": warmup_std, "n_halts": int(n_halts),
            "changepoints_sec_since_event": cps_sec,
        }
    except Exception as e:
        import traceback
        return {**base, "status": "error", "error": str(e), "traceback": traceback.format_exc()}


def _run_pelt(active_seconds, wji_log, t_event_active, sigma_log) -> tuple[list, int]:
    """Resample WJI_log to a uniform active-time grid and run rbf PELT; cap at 10 CPs."""
    t0 = float(active_seconds[0]); dur = float(active_seconds[-1] - t0)
    if dur <= 0:
        return [], 0
    bin_w = max(2.0, dur / PELT_TARGET_BINS)
    n_bins = int(math.ceil(dur / bin_w)) + 1
    idx = np.clip(((active_seconds - t0) / bin_w).astype(int), 0, n_bins - 1)
    sums = np.zeros(n_bins); counts = np.zeros(n_bins)
    np.add.at(sums, idx, wji_log); np.add.at(counts, idx, 1.0)
    grid = np.full(n_bins, np.nan)
    nz = counts > 0; grid[nz] = sums[nz] / counts[nz]
    last = grid[0] if not np.isnan(grid[0]) else 0.0
    for i in range(n_bins):
        if np.isnan(grid[i]):
            grid[i] = last
        else:
            last = grid[i]

    algo = rpt.Pelt(model="rbf", min_size=2).fit(grid.reshape(-1, 1))
    pen = math.log(n_bins) * (sigma_log ** 2)
    pen = max(pen, 1e-6)
    bkps = algo.predict(pen=pen)
    # scale penalty up until ≤ 10 internal changepoints (cap)
    guard = 0
    while len(bkps) - 1 > PELT_MAX_CP and guard < 40:
        pen *= 1.6
        bkps = algo.predict(pen=pen)
        guard += 1
    cps_sec = [round(t0 + b * bin_w - t_event_active, 1) for b in bkps[:-1]]
    return cps_sec, len(cps_sec)


# ══════════════════════════════════════════════════════════════════════
#  Chart builder
# ══════════════════════════════════════════════════════════════════════

def _downsample(x, *ys, max_points=PLOT_MAX_POINTS):
    """Uniform stride downsample of aligned arrays (keeps first/last)."""
    n = len(x)
    if n <= max_points:
        return (x, *ys)
    step = int(math.ceil(n / max_points))
    sl = slice(None, None, step)
    return (x[sl], *[y[sl] for y in ys])


def _pass_intervals(tse, state_code):
    """Return list of (start_sec, end_sec) active-sec-since-event where state == PASS."""
    intervals = []
    in_pass = False; start = None
    for i in range(len(state_code)):
        if state_code[i] == 2 and not in_pass:
            in_pass = True; start = float(tse[i])
        elif state_code[i] != 2 and in_pass:
            in_pass = False; intervals.append((start, float(tse[i])))
    if in_pass:
        intervals.append((start, float(tse[-1])))
    return intervals


def _build_chart(ticker, date, tse, prices, wji, wji_log, s_up, state_code,
                 entries, exits, trade_pnls, sigma_log, cps_sec, n_halts,
                 total_pnl, tail_label, path):
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    import plotly.io as pio

    # Restrict the visible window to post-T_event (the EPG/tradeable region).
    post = tse >= 0.0
    tv = tse[post]; pv = prices[post]
    wv = wji[post]; wlv = wji_log[post]; sv = s_up[post]; stv = state_code[post]
    pass_iv = _pass_intervals(tv, stv)

    # 1-min (60 active-sec) OHLC candles anchored to T_event
    if len(tv):
        bsz = 60.0
        bidx = (tv / bsz).astype(int)
        ub = np.unique(bidx)
        o = np.array([pv[bidx == b][0] for b in ub])
        h = np.array([pv[bidx == b].max() for b in ub])
        lo = np.array([pv[bidx == b].min() for b in ub])
        c = np.array([pv[bidx == b][-1] for b in ub])
        cx = ub * bsz + bsz / 2.0
    else:
        o = h = lo = c = cx = np.array([])

    fig = make_subplots(
        rows=5, cols=1, shared_xaxes=True, vertical_spacing=0.035,
        row_heights=[0.30, 0.18, 0.18, 0.18, 0.16],
        subplot_titles=("Price (1-min candles)", "WJI  (background ≡ 1.0)",
                        "log(WJI / background) = log(WJI)", "CUSUM S_up  (k=12, h=8)",
                        "Gate state"),
    )

    # Panel 1 — candles + markers
    fig.add_trace(go.Candlestick(x=cx, open=o, high=h, low=lo, close=c,
                                 name="price", showlegend=False), row=1, col=1)
    if entries:
        ex = [e[0] for e in entries]; ep = [e[1] for e in entries]
        fig.add_trace(go.Scatter(x=ex, y=ep, mode="markers",
                                 marker=dict(symbol="triangle-up", color="green", size=11),
                                 name="entry", showlegend=False), row=1, col=1)
    for k, (xt, xp) in enumerate(exits):
        win = (trade_pnls[k] > 0) if k < len(trade_pnls) else False
        fig.add_trace(go.Scatter(x=[xt], y=[xp], mode="markers",
                                 marker=dict(symbol="triangle-down",
                                             color=("green" if win else "red"), size=11),
                                 showlegend=False), row=1, col=1)

    # Panels 2–4 — downsampled signal lines
    dt, dw, dwl, dsu = _downsample(tv, wv, wlv, sv)
    fig.add_trace(go.Scatter(x=dt, y=dw, mode="lines", line=dict(color="#4C78A8", width=1),
                             name="WJI", showlegend=False), row=2, col=1)
    fig.add_hline(y=1.0, line=dict(color="grey", width=1, dash="dash"), row=2, col=1)
    fig.add_trace(go.Scatter(x=dt, y=dwl, mode="lines", line=dict(color="#4C78A8", width=1),
                             name="WJI_log", showlegend=False), row=3, col=1)
    fig.add_hline(y=0.0, line=dict(color="grey", width=1, dash="dash"), row=3, col=1)
    fig.add_hline(y=sigma_log, line=dict(color="#7FC7FF", width=1, dash="dot"), row=3, col=1)
    fig.add_hline(y=-sigma_log, line=dict(color="#7FC7FF", width=1, dash="dot"), row=3, col=1)
    fig.add_trace(go.Scatter(x=dt, y=dsu, mode="lines", line=dict(color="#B279A2", width=1),
                             name="S_up", showlegend=False), row=4, col=1)
    fig.add_hline(y=H, line=dict(color="red", width=1.2, dash="dash"),
                  annotation_text="h=8", row=4, col=1)
    fig.add_hline(y=0.0, line=dict(color="grey", width=1, dash="dot"), row=4, col=1)

    # Panel 5 — state step
    dts, dstv = _downsample(tv, stv)
    fig.add_trace(go.Scatter(x=dts, y=dstv, mode="lines", line=dict(color="#555", width=1, shape="hv"),
                             showlegend=False), row=5, col=1)
    fig.update_yaxes(tickvals=[0, 1, 2], ticktext=["FAIL", "WARMUP", "PASS"], row=5, col=1)

    # PELT changepoints (orange dashed) on panels 2 & 3; visible-window only
    for cp in cps_sec:
        if cp is None or cp < 0 or (len(tv) and cp > tv[-1]):
            continue
        for r in (2, 3):
            fig.add_vline(x=cp, line=dict(color="orange", width=1, dash="dash"), row=r, col=1)

    # Cross-panel shading: WARMUP [0,300] yellow; PASS windows green
    for r in range(1, 6):
        fig.add_vrect(x0=0.0, x1=EPG_WARMUP, fillcolor="#FFF3B0", opacity=0.25,
                      line_width=0, row=r, col=1)
        for (a, b) in pass_iv:
            fig.add_vrect(x0=a, x1=b, fillcolor="#9BE19B", opacity=0.15,
                          line_width=0, row=r, col=1)
    fig.add_vline(x=0.0, line=dict(color="black", width=1, dash="dash"), row=1, col=1)

    fig.update_layout(
        height=1250, width=1250, template="plotly_white",
        xaxis5=dict(title="active seconds since T_event"),
        title=(f"{ticker} {date} | k=12 h=8 | n_trades={len(trade_pnls)} | "
               f"PnL={total_pnl:.1f}% | CVaR5 contribution: {tail_label}"
               + (f" | halts={n_halts}" if n_halts else "")),
        showlegend=False,
    )
    fig.update_xaxes(rangeslider_visible=False)
    pio.write_html(fig, str(path), include_plotlyjs=True, auto_open=False)


# ══════════════════════════════════════════════════════════════════════
#  Index page
# ══════════════════════════════════════════════════════════════════════

def _write_index(rows: list[dict], path: Path):
    cols = [("ticker", "ticker"), ("date", "date"), ("year", "year"),
            ("n_trades", "n_trades"), ("total_pnl_pct", "total_pnl"),
            ("worst_trade_pnl_pct", "worst"), ("tail_label", "tail"),
            ("n_cusum_pass_windows", "pass_win"), ("n_pelt_changepoints", "cps")]
    head = "".join(f"<th onclick='sortBy({i})'>{label}</th>" for i, (_, label) in enumerate(cols))
    trs = []
    for r in rows:
        link = f"{r['ticker']}_{r['date']}.html"
        cells = [f"<td><a href='{link}'>{r['ticker']}</a></td>", f"<td>{r['date']}</td>",
                 f"<td>{r['year']}</td>", f"<td>{r['n_trades']}</td>",
                 f"<td>{r['total_pnl_pct']:.1f}</td>", f"<td>{r['worst_trade_pnl_pct']:.1f}</td>",
                 f"<td>{r['tail_label']}</td>", f"<td>{r['n_cusum_pass_windows']}</td>",
                 f"<td>{r['n_pelt_changepoints']}</td>"]
        trs.append("<tr>" + "".join(cells) + "</tr>")
    html = f"""<!doctype html><html><head><meta charset='utf-8'><title>CPD-DIAG k12_h8</title>
<style>body{{font-family:sans-serif;margin:24px}}table{{border-collapse:collapse}}
th,td{{border:1px solid #ccc;padding:6px 10px;text-align:right}}th{{cursor:pointer;background:#eee}}
td:first-child,th:first-child{{text-align:left}}tr:nth-child(even){{background:#f7f7f7}}</style></head>
<body><h2>CPD-DIAG — CUSUM k=12 h=8 — 15 events (seed=7)</h2>
<p>WJI_background ≡ 1.0 · axis = active sec since T_event · click a header to sort</p>
<table id='t'><thead><tr>{head}</tr></thead><tbody>{''.join(trs)}</tbody></table>
<script>
function sortBy(c){{let tb=document.querySelector('#t tbody');let rs=[...tb.rows];
let asc=tb.getAttribute('data-c')==c?tb.getAttribute('data-a')!='1':true;
rs.sort((x,y)=>{{let a=x.cells[c].innerText,b=y.cells[c].innerText;
let na=parseFloat(a),nb=parseFloat(b);if(!isNaN(na)&&!isNaN(nb)){{return asc?na-nb:nb-na;}}
return asc?a.localeCompare(b):b.localeCompare(a);}});
rs.forEach(r=>tb.appendChild(r));tb.setAttribute('data-c',c);tb.setAttribute('data-a',asc?'1':'0');}}
</script></body></html>"""
    path.write_text(html, encoding="utf-8")


# ══════════════════════════════════════════════════════════════════════
#  Driver
# ══════════════════════════════════════════════════════════════════════

def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    CHART_DIR.mkdir(parents=True, exist_ok=True)

    events = json.load(open(SAMPLE_PATH))["events"]
    cache = json.load(open(CACHE_PATH))
    lut = {(r["ticker"], r["date"]): r for r in cache if r.get("status") == "ok"}
    q_bar_cfg = json.load(open(QBAR_PATH))

    rng = np.random.default_rng(7)
    sel_idx = rng.choice(len(events), size=15, replace=False)
    selected = [{"ticker": events[i]["ticker"], "date": events[i]["date"],
                 "year": events[i]["date"][:4], "event_index_in_val_sample": int(i),
                 "mom_pct": events[i]["mom_pct"]} for i in sel_idx]
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    json.dump([{k: v for k, v in s.items() if k != "mom_pct"} for s in selected],
              open(OUT_DIR / "selected_events.json", "w"), indent=2)

    work = []
    for s in selected:
        c = lut.get((s["ticker"], s["date"]))
        if c is None:
            continue
        work.append({"ticker": s["ticker"], "date": s["date"], "mom_pct": s["mom_pct"],
                     "t_event": c["t_event"], "mu_buy": c["mu_buy"], "q_bar_cfg": q_bar_cfg,
                     "val_index": s["event_index_in_val_sample"]})

    print(f"CPD-DIAG: reconstructing {len(work)} events (k=12, h=8)")
    results = []
    t0 = time.time()
    with ProcessPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(_reconstruct, a) for a in work]
        for fut in as_completed(futs):
            r = fut.result()
            results.append(r)
            tag = r["status"]
            print(f"  {r['ticker']} {r['date']}: {tag}"
                  + (f"  n={r['n_trades']} pnl={r['total_pnl_pct']:.1f}% worst={r['worst_trade_pnl_pct']:.1f}% "
                     f"[{r['tail_label']}] cps={r['n_pelt_changepoints']}" if tag == "ok"
                     else f"  {r.get('error')}"))

    ok = [r for r in results if r["status"] == "ok"]
    errs = [r for r in results if r["status"] != "ok"]

    # signal_stats.json
    stats = [{k: r[k] for k in ("ticker", "date", "year", "val_index", "sigma_log",
              "warmup_wji_log_mean", "warmup_wji_log_std", "n_trades", "total_pnl_pct",
              "worst_trade_pnl_pct", "tail_label", "n_cusum_pass_windows",
              "n_pelt_changepoints", "n_halts")} for r in ok]
    json.dump(stats, open(OUT_DIR / "signal_stats.json", "w"), indent=2)
    json.dump({r["ticker"] + "|" + r["date"]: r["changepoints_sec_since_event"] for r in ok},
              open(OUT_DIR / "pelt_changepoints.json", "w"), indent=2)

    # index
    ok_sorted = sorted(ok, key=lambda r: r["ticker"])
    _write_index(ok_sorted, CHART_DIR / "index.html")

    # ── T2a descriptive centering check ──
    centered = sum(1 for r in ok if abs(r["warmup_wji_log_mean"]) < 0.5 * r["sigma_log"])
    print("\n── T2a WARMUP centering (DESCRIPTIVE — not a hard stop, Cooper) ──")
    print(f"  events with |mean WJI_log| < 0.5·sigma_log: {centered}/{len(ok)} "
          f"(expected LOW — warmup window is the ignition surge)")
    print(f"\n{'ticker':<8}{'date':<12}{'n':>4}{'PnL%':>8}{'worst%':>9}{'tail':>15}"
          f"{'cps':>5}{'sigma':>8}{'wmean':>8}{'wstd':>7}")
    for r in ok_sorted:
        print(f"{r['ticker']:<8}{r['date']:<12}{r['n_trades']:>4}{r['total_pnl_pct']:>8.1f}"
              f"{r['worst_trade_pnl_pct']:>9.1f}{r['tail_label']:>15}{r['n_pelt_changepoints']:>5}"
              f"{r['sigma_log']:>8.3f}{r['warmup_wji_log_mean']:>8.2f}{r['warmup_wji_log_std']:>7.2f}")
    print(f"\nDone in {time.time()-t0:.0f}s. {len(ok)} charts, {len(errs)} errors.")
    if errs:
        for r in errs:
            print(f"  ERROR {r['ticker']} {r['date']}: {r.get('error')}")
    print(f"→ {CHART_DIR}")


if __name__ == "__main__":
    main()
