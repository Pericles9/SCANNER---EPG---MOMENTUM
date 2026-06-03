"""
T6 — Charts for top 10 filtered val configs (Phase EPG-OPT2-SF).

2-panel charts (same format as OPT2 T7) for the top 10 configs by filtered val
Borda × all 20 OPT2 chart events. Panel 2 addition: periods where Q_tilde < 0.65
shaded in light red (rgba(220,50,50,0.10)) — directly shows blocked-entry windows.

Reads:
  results/phase_epg_opt2_sf/sweep_val_sf_ranked.json   — top 10 by filtered val Borda
  results/phase_epg_opt2/chart_events.json             — 20 OPT2 chart events

Writes:
  results/phase_epg_opt2_sf/charts/{config_id}/{TICKER}_{DATE}.html
  results/phase_epg_opt2_sf/charts/master_index.html
"""
from __future__ import annotations

import json
import logging
import math
import sys
import time
from pathlib import Path
from typing import Optional

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from data.loaders.trades import load_trades, compute_lambda_ref_per_event, _session_ns_bounds
from data.loaders.quotes import load_quotes
from data.schemas.mom_db import CONFIG_DIR, NS_PER_SECOND
from core.epg.anchor import EventAnchor
from core.epg.gate import ParticipationGate, GateState
from core.ofi.trade_ofi import compute_trade_ofi
from core.filters.setup_filter import Q_THRESHOLD
from tools.t3_sweep_runner import _hawkes_replay_with_refit, EPG_K, EPG_WARMUP
from tools.sweep_runner_opt2 import precompute_sf_trajectory

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
    HAS_PLOTLY = True
except ImportError:
    HAS_PLOTLY = False

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger(__name__)

CHART_EVENTS_PATH = REPO_ROOT / "results" / "phase_epg_opt2" / "chart_events.json"
VAL_RANKED_PATH = REPO_ROOT / "results" / "phase_epg_opt2_sf" / "sweep_val_sf_ranked.json"
TOP_DECILE_PATH = REPO_ROOT / "results" / "phase_epg_opt2_sf" / "top_decile_configs.json"
OUT_DIR = REPO_ROOT / "results" / "phase_epg_opt2_sf" / "charts"
TOP_N = 10


def _enrich(cfg: dict, full_params: dict) -> dict:
    """Merge full gate params (L_sec, k_open, k_close, mode for SlopeGate; etc.)
    from top_decile_configs.json into a val-ranked row, which only carries
    tau/p_open/p_close. The val row's metrics (profit_factor, borda_score) win."""
    fp = full_params.get(cfg["config_id"], {})
    merged = {**fp, **cfg}  # cfg (val metrics) takes precedence on overlap
    # but gate-structure params must come from fp (val row has them as None/absent)
    for k in ("L_sec", "k_open", "k_close", "mode", "tau", "p_open", "p_close",
              "m_cool_sec", "tau_cool_sec", "variant"):
        if fp.get(k) is not None:
            merged[k] = fp[k]
    return merged


def preprocess_event(ev: dict, hawkes_params: dict, q_bar_cfg: dict) -> Optional[dict]:
    ticker, date = ev["ticker"], ev["date"]
    mom_pct = ev.get("mom_pct", 0.3)
    try:
        td = load_trades(ticker, date, mom_pct)
        if td.n_trades < 30:
            return None
        qd = load_quotes(ticker, date, mom_pct)
        if qd is None or qd.n_quotes < 10:
            return None
        N = td.n_trades
        tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
        ofi = compute_trade_ofi(
            trade_timestamps=td.timestamps, trade_prices=td.prices,
            trade_sizes=td.sizes.astype(np.float64),
            quote_timestamps=qd.timestamps, quote_bid_prices=qd.bid_prices,
            quote_ask_prices=qd.ask_prices, quote_bid_sizes=qd.bid_sizes.astype(np.float64),
            quote_ask_sizes=qd.ask_sizes.astype(np.float64),
            window_sec=10.0, q_bar_fallback=tier_qbar,
        )
        sides = ofi.sides
        lam_buy_out = np.zeros(N); lam_sell_out = np.zeros(N)
        E_out = np.zeros(N); Edot_out = np.zeros(N); n_base_out = np.zeros(N)
        global_lref = hawkes_params["mu_buy"] + hawkes_params["mu_sell"]
        per_event_lref = compute_lambda_ref_per_event(ticker, date)
        lambda_ref = per_event_lref if not math.isnan(per_event_lref) and per_event_lref > 0 else global_lref
        cold = _hawkes_replay_with_refit(
            t_sec=td.t_sec, sides=sides, rho=hawkes_params.get("rho", 0.99),
            lambda_ref=lambda_ref, init_params=hawkes_params, rho_E=hawkes_params.get("rho", 0.99),
            lam_buy_out=lam_buy_out, lam_sell_out=lam_sell_out,
            E_out=E_out, Edot_out=Edot_out, n_base_out=n_base_out,
        )
        lambda_hat = lam_buy_out + lam_sell_out
        anchor = EventAnchor(lambda_ref=global_lref, k_multiplier=EPG_K)
        if cold is not None:
            lref_epg = cold.mu_buy + cold.mu_sell
            if lref_epg > 0:
                anchor.set_lambda_ref(lref_epg)
        t_event = None
        for i in range(N):
            t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
            if t_ev is not None:
                t_event = t_ev
                break
        if t_event is None:
            log.warning("%s %s: no T_event", ticker, date)
            return None
        start_ns, end_ns = _session_ns_bounds(date)
        sf = precompute_sf_trajectory(td, start_ns, end_ns)
        lv_ref = (cold.mu_buy + cold.mu_sell) if cold is not None else global_lref
        lv_ref = max(lv_ref, 1e-9)
        return {"ticker": ticker, "date": date, "td": td, "sides": sides,
                "t_event": t_event, "sf": sf, "lambda_v_ref": lv_ref,
                "leg_class": ev.get("leg_class", "?")}
    except Exception as e:
        log.warning("%s %s preprocess failed: %s", ticker, date, e)
        return None


def resample_10s(t_sec, prices):
    if len(t_sec) == 0:
        return [], [], [], [], []
    t0 = math.floor(t_sec[0] / 10) * 10
    t_max = t_sec[-1]
    ct, co, ch, cl, cc = [], [], [], [], []
    idx = 0; n = len(t_sec); t = t0
    while t <= t_max + 10:
        bucket = []
        while idx < n and t_sec[idx] < t + 10:
            bucket.append(prices[idx]); idx += 1
        if bucket:
            ct.append(t); co.append(bucket[0]); cc.append(bucket[-1])
            ch.append(max(bucket)); cl.append(min(bucket))
        t += 10
    return ct, co, ch, cl, cc


def build_chart(cfg: dict, ev_data: dict) -> Optional[str]:
    td = ev_data["td"]; sf = ev_data["sf"]; t_event = ev_data["t_event"]
    ticker = ev_data["ticker"]; date = ev_data["date"]
    lv_ref = ev_data.get("lambda_v_ref", 1.0)
    N = td.n_trades

    variant = cfg.get("variant", "a")
    is_slope = variant in ("f_ss", "f_sl")

    from tools.sweep_runner_opt2 import _build_gate_for_cfg
    gate = _build_gate_for_cfg(cfg, lambda_v_ref=lv_ref)
    gate.activate(t_event)

    t_sec = list(td.t_sec)
    prices = [float(td.prices[i]) for i in range(N)]
    lv_trace, thr_open, thr_close, states = [], [], [], []
    slope_trace = []
    for i in range(N):
        dv = float(td.prices[i]) * float(td.sizes[i])
        st = gate.update(dv, td.t_sec[i])
        states.append(st)
        if is_slope:
            lv_trace.append(gate._lambda_v)
            slope_trace.append(gate.norm_slope)
            thr_open.append(0.0)
            thr_close.append(0.0)
        else:
            lv_trace.append(gate._lambda_v)
            thr_open.append(gate.p_open * gate._lambda_v_peak)
            thr_close.append(gate.p_close * gate._lambda_v_peak)

    # PASS windows + entry/exit (with SF gating to mark which were taken)
    pass_windows = []; window_start = None
    in_pos = False; entry_px = None
    entries, exits, exit_colors = [], [], []
    from tools.sweep_runner_opt2 import sf_is_qualified_at
    for i, st in enumerate(states):
        prev = states[i - 1] if i > 0 else GateState.INACTIVE
        if st == GateState.PASS and prev != GateState.PASS:
            window_start = t_sec[i]
        elif st != GateState.PASS and prev == GateState.PASS:
            if window_start is not None:
                pass_windows.append((window_start, t_sec[i]))
            window_start = None
        if not in_pos:
            if st == GateState.PASS and prev in (GateState.INACTIVE, GateState.WARMUP, GateState.FAIL):
                if sf_is_qualified_at(sf, int(td.timestamps[i])):
                    entry_px = prices[min(i + 1, N - 1)]
                    entries.append((t_sec[i], entry_px))
                    in_pos = True
        else:
            if prev == GateState.PASS and st != GateState.PASS:
                xp = prices[min(i + 1, N - 1)]
                pnl = (xp - entry_px) / entry_px * 100 if entry_px else 0
                exits.append((t_sec[i], xp))
                exit_colors.append("green" if pnl >= 0 else "red")
                in_pos = False
    if window_start is not None:
        pass_windows.append((window_start, t_sec[-1]))

    # Q_tilde < 0.65 intervals (in t_sec domain) for red shading
    session_start_ns = int(td.timestamps[0]) - int(td.t_sec[0] * NS_PER_SECOND)
    sf_block_intervals = []
    if sf.n_bars > 0:
        bar_ns = 60 * NS_PER_SECOND
        for b in range(sf.n_bars):
            if not bool(sf.qualified[b]):
                bar_t0_ns = int(sf.bar_starts_ns[b])
                bar_t0_sec = (bar_t0_ns - session_start_ns) / NS_PER_SECOND
                sf_block_intervals.append((bar_t0_sec, bar_t0_sec + 60.0))

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.6, 0.4],
                        vertical_spacing=0.06,
                        subplot_titles=[f"{ticker} {date} — {cfg['config_id']}", "λ_V signal (SF-blocked = red)"])
    ct, co, ch, cl, cc = resample_10s(t_sec, prices)
    fig.add_trace(go.Candlestick(x=ct, open=co, high=ch, low=cl, close=cc,
                  increasing_line_color="green", decreasing_line_color="red",
                  showlegend=False), row=1, col=1)
    for ws, we in pass_windows:
        fig.add_vrect(x0=ws, x1=we, fillcolor="rgba(0,200,0,0.15)", line_width=0,
                      layer="below", row=1, col=1)
    for et, ep in entries:
        fig.add_trace(go.Scatter(x=[et], y=[ep], mode="markers",
                      marker=dict(symbol="triangle-up", size=10, color="lime"),
                      showlegend=False), row=1, col=1)
    for (xt, xp), c in zip(exits, exit_colors):
        fig.add_trace(go.Scatter(x=[xt], y=[xp], mode="markers",
                      marker=dict(symbol="triangle-down", size=10, color=c),
                      showlegend=False), row=1, col=1)

    # Panel 2: signal + thresholds + SF red shading (Q_tilde < 0.65 = blocked)
    for bt0, bt1 in sf_block_intervals:
        fig.add_vrect(x0=bt0, x1=bt1, fillcolor="rgba(220,50,50,0.10)", line_width=0,
                      layer="below", row=2, col=1)
    if is_slope:
        ko = cfg["k_open"]
        kc = cfg.get("k_close", -1.0)
        if kc is None:
            kc = -1.0
        fig.add_trace(go.Scatter(x=t_sec, y=slope_trace, mode="lines",
                      line=dict(color="purple", width=1), name="norm_slope"), row=2, col=1)
        fig.add_hline(y=ko, line=dict(color="green", width=1, dash="dash"),
                      annotation_text=f"k_open={ko}", row=2, col=1)
        if cfg.get("mode", "ss") == "ss":
            fig.add_hline(y=kc, line=dict(color="red", width=1, dash="dot"),
                          annotation_text=f"k_close={kc}", row=2, col=1)
            fig.add_hrect(y0=kc, y1=ko, fillcolor="rgba(128,128,128,0.10)",
                          line_width=0, layer="below", row=2, col=1)
    else:
        fig.add_trace(go.Scatter(x=t_sec, y=lv_trace, mode="lines",
                      line=dict(color="blue", width=1), name="λ_V"), row=2, col=1)
        fig.add_trace(go.Scatter(x=t_sec, y=thr_open, mode="lines",
                      line=dict(color="darkgreen", width=1, dash="dash"),
                      name=f"p_open×peak ({cfg['p_open']})"), row=2, col=1)
        if cfg.get("p_close") != cfg.get("p_open"):
            fig.add_trace(go.Scatter(x=t_sec, y=thr_close, mode="lines",
                          line=dict(color="orange", width=1, dash="dot"),
                          name=f"p_close×peak ({cfg['p_close']})"), row=2, col=1)

    fig.update_layout(height=620, margin=dict(l=50, r=30, t=60, b=40),
                      showlegend=True, xaxis_rangeslider_visible=False)
    return fig.to_html(full_html=True, include_plotlyjs="cdn")


def main() -> None:
    if not HAS_PLOTLY:
        log.error("plotly not installed")
        sys.exit(1)
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    ranked = json.load(open(VAL_RANKED_PATH))
    non_dq = [r for r in ranked if not r.get("disqualified", False)]

    # --configs id1,id2,... selects specific configs by ID (any non-DQ); else top N by Borda
    explicit_ids = None
    for a in sys.argv[1:]:
        if a.startswith("--configs="):
            explicit_ids = [x.strip() for x in a.split("=", 1)[1].split(",") if x.strip()]
    # Full gate params (SlopeGate L_sec/k_open/k_close/mode) live in top_decile_configs.json;
    # val-ranked rows only carry tau/p_open/p_close. Enrich before building gates.
    full_params = {c["config_id"]: c for c in json.load(open(TOP_DECILE_PATH))}

    if explicit_ids:
        by_id = {r["config_id"]: r for r in ranked}
        top_configs = [_enrich(by_id[i], full_params) for i in explicit_ids if i in by_id]
        missing = [i for i in explicit_ids if i not in by_id]
        if missing:
            log.warning("Config IDs not found in val ranking: %s", missing)
        log.info("Charting %d explicit configs: %s", len(top_configs),
                 [c["config_id"] for c in top_configs])
    else:
        top_configs = [_enrich(r, full_params) for r in non_dq[:TOP_N]]
        log.info("Top %d filtered val configs: %s", len(top_configs),
                 [c["config_id"] for c in top_configs])

    chart_events = json.load(open(CHART_EVENTS_PATH))
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes_params = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar_cfg = json.load(f)

    log.info("Preprocessing %d chart events...", len(chart_events))
    preprocessed = {}
    for ev in chart_events:
        preprocessed[(ev["ticker"], ev["date"])] = preprocess_event(ev, hawkes_params, q_bar_cfg)
    n_ok = sum(1 for v in preprocessed.values() if v is not None)
    log.info("Preprocessed %d/%d ok", n_ok, len(chart_events))

    n_charts = n_failed = 0
    master = []
    t0 = time.time()
    for cfg in top_configs:
        cid = cfg["config_id"]
        cdir = OUT_DIR / cid
        cdir.mkdir(exist_ok=True)
        files = []
        for ev in chart_events:
            ev_data = preprocessed.get((ev["ticker"], ev["date"]))
            if ev_data is None:
                n_failed += 1
                continue
            try:
                html = build_chart(cfg, ev_data)
                if html is None:
                    n_failed += 1
                    continue
                fn = f"{ev['ticker']}_{ev['date']}.html"
                (cdir / fn).write_text(html, encoding="utf-8")
                n_charts += 1
                files.append({"ticker": ev["ticker"], "date": ev["date"],
                              "leg_class": ev.get("leg_class", "?"), "filename": fn})
                master.append({"config_id": cid, "val_pf_sf": cfg.get("profit_factor"),
                               "borda": cfg.get("borda_score"),
                               "ticker": ev["ticker"], "date": ev["date"],
                               "leg_class": ev.get("leg_class", "?"), "filename": fn})
            except Exception as e:
                log.warning("chart failed %s %s_%s: %s", cid, ev["ticker"], ev["date"], e)
                n_failed += 1
        # per-config index
        rows_html = "\n".join(
            f'<tr><td><a href="{f["filename"]}">{f["ticker"]}</a></td><td>{f["date"]}</td><td>{f["leg_class"]}</td></tr>'
            for f in sorted(files, key=lambda x: (x["leg_class"], x["date"])))
        (cdir / "index.html").write_text(
            f"<html><head><title>{cid}</title></head><body><h2>{cid}</h2>"
            f"<table border=1><tr><th>Ticker</th><th>Date</th><th>Leg</th></tr>{rows_html}</table></body></html>",
            encoding="utf-8")
        log.info("[%s] %d charts", cid, len(files))

    # master index
    mrows = "\n".join(
        f'<tr><td><a href="{e["config_id"]}/index.html">{e["config_id"]}</a></td>'
        f'<td>{e.get("val_pf_sf")}</td><td>{e.get("borda")}</td>'
        f'<td>{e["ticker"]}</td><td>{e["date"]}</td><td>{e["leg_class"]}</td>'
        f'<td><a href="{e["config_id"]}/{e["filename"]}">view</a></td></tr>'
        for e in master)
    (OUT_DIR / "master_index.html").write_text(
        f"<html><head><title>EPG-OPT2-SF Charts</title></head><body>"
        f"<h1>EPG-OPT2-SF Charts ({n_charts})</h1>"
        f"<table border=1><tr><th>Config</th><th>val_PF_sf</th><th>Borda</th>"
        f"<th>Ticker</th><th>Date</th><th>Leg</th><th>Chart</th></tr>{mrows}</table></body></html>",
        encoding="utf-8")

    log.info("T6 complete: %d charts, %d failed, %.1fs", n_charts, n_failed, time.time() - t0)
    log.info("Master index: %s", OUT_DIR / "master_index.html")


if __name__ == "__main__":
    main()
