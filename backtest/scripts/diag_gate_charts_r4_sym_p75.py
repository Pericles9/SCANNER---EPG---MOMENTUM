#!/usr/bin/env python3
"""
Phase R1-FINAL — Gate intensity charts for val_r4_stratified, sym_p75 config.

Copy of diag_gate_charts.py adapted for:
  - val_r4_stratified.json (100 events, stratum replaces slot)
  - p_open = p_close = 0.75 (sym_p75 best config)
  - Entry/exit from phase_r1_final/sym_p75/per_trade.json
  - Failure reason for non-traded from phase_diag_entry_r4/entry_audit.json
  - Output: phase_r1_final/event_charts_sym_p75/

Usage:
    python backtest/scripts/diag_gate_charts_r4_sym_p75.py
    python backtest/scripts/diag_gate_charts_r4_sym_p75.py --overwrite
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import textwrap
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "backtest"))
sys.path.insert(0, str(PROJECT_ROOT))

from data.schemas.mom_db import CONFIG_DIR, FILTERED_DIR, NS_PER_SECOND  # noqa
from data.loaders.trades import load_trades, compute_lambda_ref_per_event  # noqa
from data.loaders.quotes import load_quotes  # noqa
from core.ofi.trade_ofi import compute_trade_ofi  # noqa
from core.epg.anchor import EventAnchor  # noqa
from core.epg.gate import ParticipationGate, GateState  # noqa
from runner_rapid import _hawkes_replay_with_refit, _build_halt_intervals, EPG_K, EPG_TAU, EPG_WARMUP  # noqa

BACKTEST   = PROJECT_ROOT / "backtest"
RESULTS    = BACKTEST / "results"

SAMPLE     = BACKTEST / "data" / "val_r4_stratified.json"
PER_TRADE  = RESULTS / "phase_r1_final" / "sym_p75" / "per_trade.json"
AUDIT      = RESULTS / "phase_diag_entry_r4" / "entry_audit.json"

OUT_ROOT   = RESULTS / "phase_r1_final" / "event_charts_sym_p75"
REPLAY_DIR = OUT_ROOT / "replay_data"
OHLC_DIR   = OUT_ROOT / "ohlc_30s"
CHART_DIR  = OUT_ROOT / "charts"

EPG_P_OPEN   = 0.75
EPG_P_CLOSE  = 0.75
EPG_TAU_PEAK = 600.0
EPG_C        = 1.5
ET           = "America/New_York"


# ── Helpers ───────────────────────────────────────────────────────────────

def _find_dir_name(ticker: str, date: str) -> str | None:
    candidates = sorted(FILTERED_DIR.glob(f"{ticker}_{date}_*"))
    return candidates[-1].name if candidates else None


def _load_config() -> tuple[dict, dict]:
    with open(CONFIG_DIR / "hawkes_params.json") as f:
        hawkes = json.load(f)
    with open(CONFIG_DIR / "q_bar_tiers.json") as f:
        q_bar = json.load(f)
    return hawkes, q_bar


def _load_trade_index() -> dict[tuple[str, str], dict]:
    """Return {(ticker, date): per_trade row} from sym_p75/per_trade.json."""
    idx = {}
    if PER_TRADE.exists():
        for t in json.load(open(PER_TRADE)):
            idx[(t["ticker"], t["date"])] = t
    return idx


def _load_failure_index() -> dict[tuple[str, str], str]:
    """Return {(ticker, date): entry_failure_reason} from diag_entry_r4."""
    idx = {}
    if AUDIT.exists():
        for a in json.load(open(AUDIT)):
            idx[(a["ticker"], a["date"])] = a.get("entry_failure_reason", "UNKNOWN")
    return idx


def _build_ev_meta(ev: dict, trade_idx: dict, fail_idx: dict) -> dict:
    """Merge val_r4 event with trade data (or failure reason if not traded)."""
    key = (ev["ticker"], ev["date"])
    trade = trade_idx.get(key)
    meta = {
        "ticker":           ev["ticker"],
        "date":             ev["date"],
        "stratum":          ev.get("stratum", "?"),
        "mom_pct":          ev["mom_pct"],
        "scanner_hit_ts_ns": ev["scanner_hit_ts_ns"],
        "entry_ts":         int(trade["entry_ts"]) if trade else None,
        "exit_ts":          int(trade["exit_ts"])  if trade else None,
        "entry_t_sec":      float(trade["entry_t_sec"]) if trade else None,
        "pnl_pct":          float(trade["pnl_pct"])     if trade else None,
        "exit_reason":      trade["exit_reason"] if trade else fail_idx.get(key, "NOT_TRADED"),
        "session_bucket":   trade.get("session_bucket", "—") if trade else "—",
    }
    return meta


# ── T2: Gate Replay ───────────────────────────────────────────────────────

def _replay_event(ev_meta: dict, dir_name: str, hawkes_median: dict, q_bar_cfg: dict) -> dict:
    ticker  = ev_meta["ticker"]
    date    = ev_meta["date"]
    mom_pct = ev_meta["mom_pct"]

    td = load_trades(ticker, date, mom_pct)
    if td is None or td.n_trades < 30:
        raise RuntimeError(f"Insufficient trades for {ticker} {date}")

    qd = load_quotes(ticker, date, mom_pct)
    if qd is None or qd.n_quotes < 10:
        raise RuntimeError(f"Insufficient quotes for {ticker} {date}")

    N = td.n_trades

    tier_qbar = q_bar_cfg.get("wide", {}).get("median", 250.0)
    ofi = compute_trade_ofi(
        trade_timestamps=td.timestamps,
        trade_prices=td.prices,
        trade_sizes=td.sizes.astype(np.float64),
        quote_timestamps=qd.timestamps,
        quote_bid_prices=qd.bid_prices,
        quote_ask_prices=qd.ask_prices,
        quote_bid_sizes=qd.bid_sizes.astype(np.float64),
        quote_ask_sizes=qd.ask_sizes.astype(np.float64),
        window_sec=10.0,
        q_bar_fallback=tier_qbar,
    )

    halt_intervals = _build_halt_intervals(td)

    lam_buy  = np.zeros(N, dtype=np.float64)
    lam_sell = np.zeros(N, dtype=np.float64)
    E_out    = np.zeros(N, dtype=np.float64)
    Edot_out = np.zeros(N, dtype=np.float64)
    nbase    = np.zeros(N, dtype=np.float64)
    dv_arr   = td.prices.astype(np.float64) * td.sizes.astype(np.float64)

    fp  = hawkes_median
    rho = hawkes_median.get("rho", 0.99)

    global_lref = fp["mu_buy"] + fp["mu_sell"]
    per_ev_lref = compute_lambda_ref_per_event(ticker, date)
    lambda_ref  = global_lref if (math.isnan(per_ev_lref) or per_ev_lref <= 0) else per_ev_lref

    cold_params = _hawkes_replay_with_refit(
        t_sec=td.t_sec, sides=ofi.sides,
        rho=rho, lambda_ref=lambda_ref,
        init_params=fp, rho_E=rho,
        lam_buy_out=lam_buy, lam_sell_out=lam_sell,
        E_out=E_out, Edot_out=Edot_out, n_base_out=nbase,
        dv_arr=dv_arr,
        halt_intervals=halt_intervals or None,
    )
    lambda_hat = lam_buy + lam_sell

    anchor = EventAnchor(lambda_ref=fp["mu_buy"] + fp["mu_sell"], k_multiplier=EPG_K)
    if cold_params is not None:
        lref_epg = cold_params.mu_buy + cold_params.mu_sell
        if lref_epg > 0:
            anchor.set_lambda_ref(lref_epg)

    gate = ParticipationGate(
        half_life_seconds=EPG_TAU,
        peak_threshold_p=EPG_P_OPEN,
        warmup_seconds=EPG_WARMUP,
        gate_mode="peak",
        tau_peak=EPG_TAU_PEAK,
        C=EPG_C,
        p_open=EPG_P_OPEN,
        p_close=EPG_P_CLOSE,
    )

    ticks         = []
    t_event_fired = False
    t_event_sec   = None

    for i in range(N):
        t_ev = anchor.update(lambda_hat[i], td.t_sec[i])
        if t_ev is not None and not t_event_fired:
            gate.activate(t_ev)
            t_event_fired = True
            t_event_sec   = float(td.t_sec[i])

        dv    = float(td.prices[i]) * float(td.sizes[i])
        state = gate.update(dv, td.t_sec[i])

        lv      = gate.lambda_v
        lv_peak = gate.lambda_v_peak
        ratio   = (lv / lv_peak) if lv_peak > 0 else 0.0

        ticks.append({
            "i":               i,
            "ts_ns":           int(td.timestamps[i]),
            "t_sec":           float(td.t_sec[i]),
            "price":           float(td.prices[i]),
            "size":            int(td.sizes[i]),
            "dv":              float(dv),
            "lambda_v":        float(lv),
            "lambda_v_peak":   float(lv_peak),
            "threshold":       float(gate.threshold),
            "threshold_close": float(gate.threshold_close),
            "ratio":           float(ratio),
            "gate_state":      state.value,
        })

    scanner_hit_t_sec = (ev_meta["scanner_hit_ts_ns"] - int(td.timestamps[0])) / NS_PER_SECOND

    return {
        "ticker":            ev_meta["ticker"],
        "date":              ev_meta["date"],
        "stratum":           ev_meta["stratum"],
        "dir_name":          dir_name,
        "session_start_ns":  int(td.timestamps[0]),
        "t_event_fired":     t_event_fired,
        "t_event_sec":       t_event_sec,
        "scanner_hit_t_sec": float(scanner_hit_t_sec),
        "n_ticks":           N,
        "ticks":             ticks,
    }


# ── T3: 30s OHLC ─────────────────────────────────────────────────────────

def _build_ohlc_30s(replay: dict) -> dict:
    ticks = replay["ticks"]

    ts_et  = pd.to_datetime([t["ts_ns"] for t in ticks], unit="ns", utc=True).tz_convert(ET)
    prices = np.array([t["price"] for t in ticks])
    sizes  = np.array([t["size"]  for t in ticks])
    dvs    = np.array([t["dv"]    for t in ticks])

    df = pd.DataFrame({"price": prices, "size": sizes, "dv": dvs}, index=ts_et)

    ohlc = df["price"].resample("30s").agg(
        Open=("first"), High=("max"), Low=("min"), Close=("last")
    )
    vol   = df["size"].resample("30s").sum()
    dv30  = df["dv"].resample("30s").sum()
    cnt30 = df["price"].resample("30s").count()

    close_cf = ohlc["Close"].ffill()
    empty    = ohlc["Open"].isna()
    for col in ("Open", "High", "Low", "Close"):
        ohlc.loc[empty, col] = close_cf[empty]

    ohlc  = ohlc.dropna(subset=["Open"])
    vol   = vol.reindex(ohlc.index).fillna(0)
    dv30  = dv30.reindex(ohlc.index).fillna(0.0)
    cnt30 = cnt30.reindex(ohlc.index).fillna(0)

    roll60  = cnt30.rolling(2,  min_periods=1).sum()
    roll300 = cnt30.rolling(10, min_periods=1).sum()

    return {
        "bar_ts":   ohlc.index.astype(str).tolist(),
        "open":     ohlc["Open"].tolist(),
        "high":     ohlc["High"].tolist(),
        "low":      ohlc["Low"].tolist(),
        "close":    ohlc["Close"].tolist(),
        "volume":   vol.tolist(),
        "dv30":     dv30.tolist(),
        "count30":  cnt30.tolist(),
        "roll60":   roll60.tolist(),
        "roll300":  roll300.tolist(),
        "n_bars":   len(ohlc),
    }


# ── T4: 4-panel Plotly chart ──────────────────────────────────────────────

def _pass_intervals_et(ticks: list[dict]) -> list[tuple]:
    intervals = []
    in_pass   = False
    start_ns  = None
    for t in ticks:
        if t["gate_state"] == "PASS" and not in_pass:
            in_pass  = True
            start_ns = t["ts_ns"]
        elif t["gate_state"] != "PASS" and in_pass:
            in_pass = False
            intervals.append((start_ns, t["ts_ns"]))
    if in_pass:
        intervals.append((start_ns, ticks[-1]["ts_ns"]))
    return intervals


def _ts(ns: int) -> pd.Timestamp:
    return pd.Timestamp(ns, unit="ns", tz="UTC").tz_convert(ET)


def _make_chart(ev_meta: dict, replay: dict, ohlc: dict) -> str:
    ticker  = replay["ticker"]
    date    = replay["date"]
    stratum = replay["stratum"]
    ticks   = replay["ticks"]

    ts_arr = pd.to_datetime([t["ts_ns"] for t in ticks], unit="ns", utc=True).tz_convert(ET)

    lv      = np.array([t["lambda_v"]      for t in ticks])
    lv_peak = np.array([t["lambda_v_peak"] for t in ticks])
    thresh  = np.array([t["threshold"]     for t in ticks])
    ratio   = np.array([t["ratio"]         for t in ticks])

    bar_ts = pd.to_datetime(ohlc["bar_ts"])

    scanner_ts = _ts(ev_meta["scanner_hit_ts_ns"])
    entry_ts   = _ts(ev_meta["entry_ts"]) if ev_meta["entry_ts"] is not None else None
    exit_ts    = _ts(ev_meta["exit_ts"])  if ev_meta["exit_ts"]  is not None else None

    fig = make_subplots(
        rows=4, cols=1,
        shared_xaxes=True,
        vertical_spacing=0.03,
        row_heights=[0.35, 0.28, 0.18, 0.19],
        specs=[
            [{"secondary_y": False}],
            [{"secondary_y": True}],
            [{"secondary_y": False}],
            [{"secondary_y": False}],
        ],
        subplot_titles=[
            f"[{stratum}] {ticker} {date}  |  30s OHLC — PASS regions shaded",
            "Gate Intensity  (λ_v / λ_v_peak / threshold)",
            "Dollar Volume per 30s bar",
            "Rolling Trade Count (60s = solid, 300s = dashed)",
        ],
    )

    # Panel 1: Candlestick + PASS shading
    fig.add_trace(
        go.Candlestick(
            x=bar_ts,
            open=ohlc["open"], high=ohlc["high"],
            low=ohlc["low"],   close=ohlc["close"],
            name="OHLC",
            increasing_line_color="#26a69a",
            decreasing_line_color="#ef5350",
            increasing_fillcolor="#26a69a",
            decreasing_fillcolor="#ef5350",
            showlegend=False,
        ),
        row=1, col=1,
    )

    for start_ns, end_ns in _pass_intervals_et(ticks):
        fig.add_vrect(
            x0=_ts(start_ns), x1=_ts(end_ns),
            fillcolor="rgba(0,200,0,0.07)",
            line_width=0,
            row=1, col=1,
        )

    # Panel 2: gate intensity
    if len(ts_arr) > 40_000:
        step = len(ts_arr) // 20_000
        ts_s, lv_s, lvp_s, thr_s, rat_s = ts_arr[::step], lv[::step], lv_peak[::step], thresh[::step], ratio[::step]
    else:
        ts_s, lv_s, lvp_s, thr_s, rat_s = ts_arr, lv, lv_peak, thresh, ratio

    fig.add_trace(go.Scatter(x=ts_s, y=lv_s, name="λ_v", mode="lines",
                             line=dict(color="#2196F3", width=1.5)), row=2, col=1)
    fig.add_trace(go.Scatter(x=ts_s, y=lvp_s, name="λ_v peak", mode="lines",
                             line=dict(color="#FF9800", width=1.5, dash="dash")), row=2, col=1)
    fig.add_trace(go.Scatter(x=ts_s, y=thr_s, name="threshold (open)", mode="lines",
                             line=dict(color="#F44336", width=1.0, dash="dash")), row=2, col=1)
    fig.add_trace(go.Scatter(x=ts_s, y=rat_s, name="ratio λ_v/peak", mode="lines",
                             line=dict(color="#9E9E9E", width=1.0)),
                  row=2, col=1, secondary_y=True)
    fig.add_trace(
        go.Scatter(
            x=[ts_arr[0], ts_arr[-1]], y=[EPG_P_CLOSE, EPG_P_CLOSE],
            mode="lines", name=f"p_close={EPG_P_CLOSE}",
            line=dict(color="rgba(244,67,54,0.6)", width=1.0, dash="dot"),
        ),
        row=2, col=1, secondary_y=True,
    )

    # Panel 3: dollar volume
    fig.add_trace(go.Bar(x=bar_ts, y=ohlc["dv30"], name="DV 30s",
                         marker_color="#546E7A", showlegend=False), row=3, col=1)

    # Panel 4: rolling trade count
    fig.add_trace(go.Scatter(x=bar_ts, y=ohlc["roll60"], name="Roll 60s count", mode="lines",
                             line=dict(color="#CE93D8", width=1.5)), row=4, col=1)
    fig.add_trace(go.Scatter(x=bar_ts, y=ohlc["roll300"], name="Roll 300s count", mode="lines",
                             line=dict(color="#7B1FA2", width=1.5, dash="dash")), row=4, col=1)

    # Vertical event lines (scanner always; entry+exit only if traded)
    event_lines = [(scanner_ts, "#2196F3", "Scanner hit")]
    if entry_ts is not None:
        event_lines.append((entry_ts, "#FF9800", "Entry"))
    if exit_ts is not None:
        event_lines.append((exit_ts, "#F44336", "Exit"))

    for ts_val, color, label in event_lines:
        for row in (1, 2, 3, 4):
            fig.add_vline(x=ts_val, line=dict(color=color, width=1.5, dash="dot"),
                          row=row, col=1)

    fig.update_yaxes(range=[0, 1.15], row=2, col=1, secondary_y=True,
                     title_text="ratio", showgrid=False)
    fig.update_yaxes(title_text="λ",       row=2, col=1, secondary_y=False)
    fig.update_yaxes(title_text="price ($)", row=1, col=1)
    fig.update_yaxes(title_text="DV ($)",   row=3, col=1)
    fig.update_yaxes(title_text="count",    row=4, col=1)

    pnl_pct     = ev_meta.get("pnl_pct")
    exit_reason = ev_meta.get("exit_reason", "")
    pnl_str     = f"{pnl_pct:+.2f}%" if pnl_pct is not None else "—"
    title_text  = (
        f"[{stratum}] {ticker} {date}  |  mom={ev_meta['mom_pct']:.1f}%  "
        f"PnL={pnl_str}  exit={exit_reason}  (p_open=p_close={EPG_P_CLOSE})"
    )

    fig.update_layout(
        template="plotly_dark",
        height=1400, width=1200,
        title=dict(text=title_text, font=dict(size=13), x=0.01),
        showlegend=True,
        legend=dict(x=1.06, y=1.0, bgcolor="rgba(0,0,0,0.4)",
                    font=dict(size=10), borderwidth=0),
        xaxis_rangeslider_visible=False,
        xaxis4_rangeslider_visible=False,
        margin=dict(l=60, r=180, t=60, b=40),
    )
    for i in range(1, 5):
        key = f"xaxis{'' if i == 1 else i}"
        fig.update_layout(**{key: dict(rangeslider_visible=False)})

    return fig.to_html(full_html=True, include_plotlyjs="cdn")


# ── T2b: entry lag check ──────────────────────────────────────────────────

def _t2b_check(ev_meta: dict, replay: dict) -> bool:
    entry_t_sec   = ev_meta.get("entry_t_sec")
    scanner_t_sec = replay.get("scanner_hit_t_sec")
    if entry_t_sec is None:
        return True  # not traded — skip check
    if scanner_t_sec is None:
        print(f"  T2b WARN [{ev_meta['stratum']}] missing scanner timestamp")
        return False
    ok  = entry_t_sec >= scanner_t_sec
    lag = entry_t_sec - scanner_t_sec
    print(f"  T2b [{ev_meta['stratum']}] {ev_meta['ticker']} {ev_meta['date']}: "
          f"entry_t_sec={entry_t_sec:.1f}  scanner_t_sec={scanner_t_sec:.1f}  "
          f"lag={lag:.1f}s  -> {'OK' if ok else 'FAIL'}")
    return ok


# ── T5: index.html ────────────────────────────────────────────────────────

def _make_index(events_meta: list[dict]) -> str:
    rows = []
    for ev in events_meta:
        pnl      = ev.get("pnl_pct")
        pnl_str  = f"{pnl:+.2f}%" if pnl is not None else "—"
        pnl_val  = f"{pnl:.6f}" if pnl is not None else "-9999"
        fname    = f"{ev['ticker']}_{ev['date']}.html"
        session  = ev.get("session_bucket", "—")
        rows.append(
            f"<tr>"
            f"<td>{ev['stratum']}</td>"
            f"<td>{session}</td>"
            f"<td>{ev['ticker']}</td>"
            f"<td>{ev['date']}</td>"
            f"<td>{ev.get('exit_reason', '—')}</td>"
            f"<td style='text-align:right' data-val='{pnl_val}'>{pnl_str}</td>"
            f"<td><a href='{fname}' target='_blank'>{fname}</a></td>"
            f"</tr>"
        )
    tbody = "\n".join(rows)
    return textwrap.dedent(f"""\
        <!DOCTYPE html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>R1-Final sym_p75 charts</title>
          <style>
            body  {{ font-family: monospace; background:#111; color:#ddd; padding:2em }}
            h2    {{ color:#eee }}
            table {{ border-collapse:collapse; width:auto }}
            th,td {{ border:1px solid #333; padding:6px 12px }}
            th    {{ background:#222; color:#aaa; cursor:pointer; user-select:none }}
            th:hover          {{ background:#333; color:#fff }}
            th.sorted-asc::after  {{ content:" ▲" }}
            th.sorted-desc::after {{ content:" ▼" }}
            a  {{ color:#90caf9 }}
          </style>
        </head>
        <body>
          <h2>Phase R1-Final — sym_p75 Gate Intensity Charts</h2>
          <p>p_open = p_close = {EPG_P_OPEN}  |  EPG tau = {EPG_TAU}s  |  warmup = {EPG_WARMUP}s  |  max_entry_lag = 500s
          <br>Click any column header to sort. Click again to reverse.</p>
          <table id="trade-table">
            <thead>
              <tr>
                <th onclick="sortTable(0)">Stratum</th>
                <th onclick="sortTable(1)">Session</th>
                <th onclick="sortTable(2)">Ticker</th>
                <th onclick="sortTable(3)">Date</th>
                <th onclick="sortTable(4)">Exit / Failure Reason</th>
                <th onclick="sortTable(5)">PnL%</th>
                <th>Chart</th>
              </tr>
            </thead>
            <tbody>
        {tbody}
            </tbody>
          </table>
          <script>
            let _col = -1, _asc = true;
            function sortTable(col) {{
              const tbl  = document.getElementById('trade-table');
              const rows = Array.from(tbl.querySelectorAll('tbody tr'));
              _asc = (_col === col) ? !_asc : true;
              _col = col;
              rows.sort((a, b) => {{
                const av = a.cells[col].dataset.val ?? a.cells[col].textContent.trim();
                const bv = b.cells[col].dataset.val ?? b.cells[col].textContent.trim();
                const an = parseFloat(av), bn = parseFloat(bv);
                if (!isNaN(an) && !isNaN(bn)) return _asc ? an - bn : bn - an;
                return _asc ? av.localeCompare(bv) : bv.localeCompare(av);
              }});
              rows.forEach(r => tbl.querySelector('tbody').appendChild(r));
              tbl.querySelectorAll('thead th').forEach((th, i) => {{
                th.classList.remove('sorted-asc', 'sorted-desc');
                if (i === col) th.classList.add(_asc ? 'sorted-asc' : 'sorted-desc');
              }});
            }}
          </script>
        </body>
        </html>
    """)


# ── Gate summary stats ────────────────────────────────────────────────────

def _gate_stats(replay: dict) -> dict:
    ticks  = replay["ticks"]
    ratios = [t["ratio"] for t in ticks if t["gate_state"] != "INACTIVE"]
    if not ratios:
        return {}
    n_ptf = 0
    total_pass_sec = 0.0
    in_pass = False
    pass_start = None
    for t in ticks:
        st = t["gate_state"]
        if st == "PASS" and not in_pass:
            in_pass    = True
            pass_start = t["t_sec"]
        elif st != "PASS" and in_pass:
            in_pass = False
            n_ptf  += 1
            total_pass_sec += t["t_sec"] - pass_start
    if in_pass:
        total_pass_sec += ticks[-1]["t_sec"] - pass_start
    return {
        "mean_ratio":              round(float(np.mean(ratios)), 4),
        "min_ratio":               round(float(np.min(ratios)),  4),
        "n_pass_to_fail":          n_ptf,
        "total_pass_duration_sec": round(total_pass_sec, 1),
    }


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--overwrite", action="store_true",
                        help="Regenerate charts even if cached files exist")
    args = parser.parse_args()

    for d in (REPLAY_DIR, OHLC_DIR, CHART_DIR):
        d.mkdir(parents=True, exist_ok=True)

    events_raw  = json.load(open(SAMPLE))["events"]
    trade_idx   = _load_trade_index()
    fail_idx    = _load_failure_index()
    hawkes_med, q_bar_cfg = _load_config()

    total = len(events_raw)
    n_traded = sum(1 for e in events_raw if (e["ticker"], e["date"]) in trade_idx)
    print(f"Phase R1-Final sym_p75 — gate charts for {total} events ({n_traded} traded)")
    print(f"Output: {OUT_ROOT}")
    print()

    all_t2b_ok  = True
    summary_rows = []
    events_meta  = []

    for idx, ev_raw in enumerate(events_raw, 1):
        ticker = ev_raw["ticker"]
        date   = ev_raw["date"]
        label  = f"[{ev_raw.get('stratum','?')}] {ticker} {date}"
        print(f"{'=' * 60}")
        print(f"  {label}")

        dir_name = _find_dir_name(ticker, date)
        if dir_name is None:
            print(f"  SKIP — no event directory found")
            continue

        ev_meta = _build_ev_meta(ev_raw, trade_idx, fail_idx)
        events_meta.append(ev_meta)

        # T2: Replay
        replay_path = REPLAY_DIR / f"{ticker}_{date}.json"
        if replay_path.exists() and not args.overwrite:
            print(f"  T2  replay ... CACHED ({replay_path.name})", flush=True)
            with open(replay_path) as f:
                replay = json.load(f)
            print(f"       ({replay['n_ticks']:,} ticks loaded from disk)")
        else:
            print(f"  T2  replay ...", end=" ", flush=True)
            try:
                replay = _replay_event(ev_meta, dir_name, hawkes_med, q_bar_cfg)
            except Exception as e:
                print(f"ERROR — {e}")
                continue
            with open(replay_path, "w") as f:
                json.dump(replay, f, indent=2)
            print(f"OK  ({replay['n_ticks']:,} ticks -> {replay_path.name})")

        # T2b: entry lag check (traded events only)
        ok = _t2b_check(ev_meta, replay)
        if not ok:
            all_t2b_ok = False

        stats = _gate_stats(replay)
        print(f"  Stats: mean_ratio={stats.get('mean_ratio')}  "
              f"min_ratio={stats.get('min_ratio')}  "
              f"pass_to_fail={stats.get('n_pass_to_fail')}  "
              f"total_pass_sec={stats.get('total_pass_duration_sec')}")

        # T3: 30s OHLC
        ohlc_path  = OHLC_DIR  / f"{ticker}_{date}.json"
        chart_path = CHART_DIR / f"{ticker}_{date}.html"

        if ohlc_path.exists() and not args.overwrite:
            print(f"  T3  OHLC 30s ... CACHED ({ohlc_path.name})")
            with open(ohlc_path) as f:
                ohlc = json.load(f)
        else:
            print(f"  T3  OHLC 30s ...", end=" ", flush=True)
            ohlc = _build_ohlc_30s(replay)
            with open(ohlc_path, "w") as f:
                json.dump(ohlc, f, indent=2)
            print(f"OK  ({ohlc['n_bars']} bars -> {ohlc_path.name})")

        # T4: Chart
        if chart_path.exists() and not args.overwrite:
            print(f"  T4  chart ... CACHED ({chart_path.name})")
        else:
            print(f"  T4  chart ...", end=" ", flush=True)
            html = _make_chart(ev_meta, replay, ohlc)
            with open(chart_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"OK  -> {chart_path.name}")

        summary_rows.append({**ev_meta, **stats})
        print()

    # T5: index
    print("T5  writing index.html ...", end=" ", flush=True)
    index_html = _make_index(events_meta)
    with open(CHART_DIR / "index.html", "w", encoding="utf-8") as f:
        f.write(index_html)
    print(f"OK -> {CHART_DIR / 'index.html'}")

    print()
    print("=" * 70)
    print("sym_p75 chart summary")
    print("=" * 70)
    hdr = f"{'Strat':5s} {'Ticker':8s} {'Date':12s} {'Exit/Reason':25s} {'PnL%':8s} {'mean_r':7s} {'min_r':6s} {'PTF':4s} {'pass_s':8s}"
    print(hdr)
    print("-" * len(hdr))
    for r in summary_rows:
        pnl_str = f"{r['pnl_pct']:+8.2f}" if r.get("pnl_pct") is not None else "        —"
        print(
            f"{r['stratum']:5s} {r['ticker']:8s} {r['date']:12s} "
            f"{r.get('exit_reason',''):25s} {pnl_str}  "
            f"{r.get('mean_ratio', 0):7.4f} {r.get('min_ratio', 0):6.4f}  "
            f"{r.get('n_pass_to_fail', 0):3d}  {r.get('total_pass_duration_sec', 0):8.1f}"
        )

    print()
    print(f"T2b all entries after scanner hit: {'PASS' if all_t2b_ok else 'FAIL'}")
    print()
    print(f"Output: {CHART_DIR}")
    print(f"Index:  {CHART_DIR / 'index.html'}")
    print("Done.")


if __name__ == "__main__":
    main()
